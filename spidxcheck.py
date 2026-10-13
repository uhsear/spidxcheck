#!/usr/bin/env python
"""Compare a spatial index box, a stored extent and the data envelope, and say where they disagree.

A disagreement is worded as a speed and display problem. It is never worded as lost rows.

A feature class gets a stored extent that was never set from its data, so it sits at
the false origin or covers a whole coordinate system. The spatial index box is built
from that extent, so the box is as large as a continent. Every query still returns
the right features, because the exact test runs after the index. The index narrows
nothing, so each query is slower than it should be, and the service full extent
opens a map over empty space. No error is raised anywhere.

Three numbers describe one layer and no tool lines them up: the box the spatial
index was built on, the extent the geodatabase stored, and the envelope of the
features that are really there. This tool takes the three, from CSV files that a
read-only query exported or from the REST endpoint of a hosted layer, and reports
which one is wrong and by how much. It never connects to a database.

    python spidxcheck.py --self-test
    python spidxcheck.py --sql
    python spidxcheck.py boxes.csv extents.csv envelopes.csv
    python spidxcheck.py boxes.csv envelopes.csv --false-origin=-5000000,-9000000
    python spidxcheck.py boxes.csv envelopes.csv --script-out rebuild.sql --apply
    python spidxcheck.py --rest http://host/arcgis/rest/services/Roads/FeatureServer/0

--sql prints the export queries. Only --apply writes a file, and the only file it
writes is the rebuild script named by --script-out. The script is text for a human
to review. The tool never runs it.

Exit codes: 0 every layer agrees, 1 at least one layer has a finding, 2 a file could
not be read or written or the server could not be read, 4 nothing could be
confirmed (a layer had too little to compare, or only the stored extent was
compared), 64 usage error, 70 an unexpected crash, which is never a finding.
"""

from __future__ import print_function

import argparse
import contextlib
import csv
import http.client
import io
import json
import math
import os
import re
import shutil
import sys
import tempfile
import traceback
import urllib.error
import urllib.parse
import urllib.request

# =============================================================================
# CONFIGURATION. Thresholds are flags where an operator may reasonably differ.
# =============================================================================

# A box or extent is oversize when its larger side is more than this many times
# the larger side of what it should cover. 10 is a judgement, not a measurement.
# A well set box is a few percent larger than the data, so 10 leaves a wide margin
# and a continent-sized box clears it by orders of magnitude.
DEFAULT_OVERSIZE_RATIO = 10.0

# Two catalogs hold the same real extent as floats that differ in the last digits.
# The tolerance is this fraction of the data's larger side, so a true miss of even
# a few feet still trips it and round-trip noise does not.
DEFAULT_TOLERANCE = 1e-4

# The rebuild script grows the data envelope by this fraction of its larger side,
# so features near the edge stay inside the new box.
DEFAULT_PAD = 0.05

# A corner counts as sitting on the false origin when it is this close, in the
# units of the coordinates. A default extent sits on it exactly.
FALSE_ORIGIN_TOL = 1.0

DEFAULT_TIMEOUT = 15.0

# The largest REST answer that is read. An extent answer is a few hundred bytes.
MAX_BODY = 5 * 1024 * 1024

OK = "OK"
FINDING = "FINDING"
UNVERIFIED = "UNVERIFIED"
UNKNOWN = "UNKNOWN"

BOX_CODES = ("FALSE_ORIGIN_BOX", "BOX_MISSES_DATA", "BOX_TOO_BIG")

NUMBER = re.compile(r"[+-]?([0-9]+\.?[0-9]*|\.[0-9]+)([eE][+-]?[0-9]+)?", re.ASCII)
DIGITS = re.compile(r"[0-9]+", re.ASCII)
GRID_NAMES = {"16": "LOW", "64": "MEDIUM", "256": "HIGH"}

# What each kind of comparison is called, its three codes, and what each means.
# Every sentence says what is slower or where a map opens, and none says rows
# are gone: Esri documents that a feature outside the index box is still
# returned by a spatial query.
KINDS = {
    "BOX": (
        "index box", "FALSE_ORIGIN_BOX", "BOX_MISSES_DATA", "BOX_TOO_BIG",
        "the index box starts at the false origin (%s, %s). ArcGIS uses the "
        "full coordinate range of the spatial reference when no extent is "
        "set, and that range starts at the false origin. Results stay "
        "correct; queries are slower.",
        "the index box does not contain the %s (short by %s in x and %s in "
        "y). Features outside the box are not indexed, so the index cannot "
        "narrow a query for them. They are still returned by spatial "
        "queries, so results stay correct; those queries are slower.",
        "the index box is %s times the %s across. The features share few "
        "grid cells, so a spatial filter keeps many candidate rows for the "
        "exact test. Results stay correct; queries are slower."),
    "EXTENT": (
        "stored extent", "FALSE_ORIGIN_EXTENT", "EXTENT_CLIPS_DATA",
        "EXTENT_TOO_BIG",
        "the stored extent starts at the false origin (%s, %s), so it looks "
        "like a default that was never set from the data. A map or service "
        "that opens on the full extent opens over empty space.",
        "the stored extent does not contain the %s (short by %s in x and %s "
        "in y). A map or service that opens on the full extent shows only "
        "part of the data, and an index box rebuilt from this extent would "
        "leave the rest outside the box.",
        "the stored extent is %s times the %s across. A map or service that "
        "opens on the full extent opens over empty space, and an index box "
        "rebuilt from it is just as oversize."),
}

SQL_TEXT = """\
-- spidxcheck export queries. Read only. Run each one and save the result as a
-- CSV with a header row. The column names matter; the tool joins on "layer".
-- These are printed text. They were checked against Microsoft's documentation
-- and never run against a SQL Server instance.

-- 1. boxes.csv: the box each spatial index was built on.
--    sys.spatial_index_tessellations holds bounding_box_xmin..ymax. They are
--    NULL for a geography index, which has no box.
SELECT SCHEMA_NAME(t.schema_id) + N'.' + t.name AS layer,
       i.name AS index_name,
       col.name AS spatial_column,
       tt.tessellation_scheme,
       tt.bounding_box_xmin AS box_xmin, tt.bounding_box_ymin AS box_ymin,
       tt.bounding_box_xmax AS box_xmax, tt.bounding_box_ymax AS box_ymax,
       tt.level_1_grid, tt.level_2_grid, tt.level_3_grid, tt.level_4_grid,
       tt.cells_per_object
FROM sys.spatial_indexes AS si
JOIN sys.indexes AS i
  ON i.object_id = si.object_id AND i.index_id = si.index_id
JOIN sys.tables AS t ON t.object_id = si.object_id
JOIN sys.index_columns AS ic
  ON ic.object_id = i.object_id AND ic.index_id = i.index_id
JOIN sys.columns AS col
  ON col.object_id = ic.object_id AND col.column_id = ic.column_id
JOIN sys.spatial_index_tessellations AS tt
  ON tt.object_id = si.object_id AND tt.index_id = si.index_id;

-- 2. extents.csv: the extent the geodatabase stored for each feature class.
--    Esri lists sde.SDE_layers with minx, miny, maxx and maxy in its older
--    system-table documentation, and does not define them. Check the columns
--    in your own release first. Never edit this table: Esri says the system
--    tables may be changed only through ArcGIS software or the SDK.
SELECT owner + N'.' + table_name AS layer,
       minx AS ext_xmin, miny AS ext_ymin, maxx AS ext_xmax, maxy AS ext_ymax
FROM sde.SDE_layers;

-- 3. envelopes.csv: the real envelope of the features, one table at a time.
--    This reads every row of the table, so run it for one table and in quiet
--    hours. EnvelopeAggregate returns a geometry bounding box, or NULL for an
--    empty table. STPointN returns NULL past the last point, so a layer of one
--    point or one straight line still gives a box.
SELECT N'<schema>.<table>' AS layer,
       (SELECT MIN(v) FROM (VALUES (g.STPointN(1).STX), (g.STPointN(2).STX),
          (g.STPointN(3).STX), (g.STPointN(4).STX)) AS p(v)) AS env_xmin,
       (SELECT MIN(v) FROM (VALUES (g.STPointN(1).STY), (g.STPointN(2).STY),
          (g.STPointN(3).STY), (g.STPointN(4).STY)) AS p(v)) AS env_ymin,
       (SELECT MAX(v) FROM (VALUES (g.STPointN(1).STX), (g.STPointN(2).STX),
          (g.STPointN(3).STX), (g.STPointN(4).STX)) AS p(v)) AS env_xmax,
       (SELECT MAX(v) FROM (VALUES (g.STPointN(1).STY), (g.STPointN(2).STY),
          (g.STPointN(3).STY), (g.STPointN(4).STY)) AS p(v)) AS env_ymax
FROM (SELECT geometry::EnvelopeAggregate([<geometry_column>]) AS g
      FROM [<schema>].[<table>]) AS e
WHERE g IS NOT NULL;
"""


# ------------------------------------------------------------------ the core
# Everything from here to the self-test is pure: no file, no socket, no clock.

class Layer(object):
    """One layer and up to three boxes: index box, stored extent, data envelope.

    Each box is a tuple (xmin, ymin, xmax, ymax) or None when it was not given.
    """

    def __init__(self, name):
        self.name = name
        self.box = None
        self.ext = None
        self.env = None
        self.index_name = ""
        self.column = ""
        self.scheme = ""
        self.grids = ("", "", "", "")
        self.cells = ""
        self.notes = []


class Result(object):
    def __init__(self, layer, status, findings, reason, notes):
        self.layer = layer
        self.status = status
        self.findings = findings
        self.reason = reason
        self.notes = notes


def size(box):
    """The larger side of a box."""
    return max(box[2] - box[0], box[3] - box[1])


def shortfall(outer, inner):
    """How far inner sticks out of outer, along x and along y. Zero if inside."""
    return (max(outer[0] - inner[0], inner[2] - outer[2], 0.0),
            max(outer[1] - inner[1], inner[3] - outer[3], 0.0))


def at_origin(box, origin):
    return (abs(box[0] - origin[0]) <= FALSE_ORIGIN_TOL
            and abs(box[1] - origin[1]) <= FALSE_ORIGIN_TOL)


def number(value):
    return "%.10g" % value


def compare(kind, outer, inner, inner_name, ratio, tol, origin):
    """Findings for one box (outer) that should cover another (inner)."""
    label, c_origin, c_miss, c_big, t_origin, t_miss, t_big = KINDS[kind]
    if origin is not None and at_origin(outer, origin):
        return [(c_origin, t_origin % (number(origin[0]), number(origin[1])))]
    found = []
    s = size(inner)
    sx, sy = shortfall(outer, inner)
    if sx > tol * s or sy > tol * s:
        found.append((c_miss, t_miss % (inner_name, "%.6g" % sx, "%.6g" % sy)))
    # A layer of one point has no size to be many times, so it is never oversize.
    if s > 0 and size(outer) / s > ratio:
        found.append((c_big, t_big % ("%.4g" % (size(outer) / s), inner_name)))
    return found


def assess(layer, ratio=DEFAULT_OVERSIZE_RATIO, tol=DEFAULT_TOLERANCE,
           origin=None):
    """Decide one layer. Never calls a layer OK when it compared nothing."""
    notes = list(layer.notes)
    box = layer.box
    if box is not None and layer.scheme.upper().startswith("GEOGRAPHY"):
        box = None
        notes.append("a geography index has no bounding box, so the index "
                     "box was not compared")
    if layer.env is not None:
        ref, ref_name = layer.env, "data envelope"
    else:
        ref, ref_name = layer.ext, "stored extent"
    if ref is None:
        return Result(layer, UNKNOWN, [], "no data envelope and no stored "
                      "extent was given", notes)
    findings = []
    compared = False
    if box is not None:
        compared = True
        findings.extend(compare("BOX", box, ref, ref_name, ratio, tol, origin))
    if layer.env is not None and layer.ext is not None:
        compared = True
        findings.extend(compare("EXTENT", layer.ext, layer.env,
                                "data envelope", ratio, tol, origin))
    if not compared:
        return Result(layer, UNKNOWN, [], "only one of the index box, the "
                      "stored extent and the data envelope was given, so "
                      "there is nothing to compare", notes)
    if findings:
        return Result(layer, FINDING, findings, "", notes)
    if ref_name == "stored extent":
        return Result(layer, UNVERIFIED, [], "the index box agrees with the "
                      "stored extent, but no data envelope was given, so "
                      "nothing measured the features. A stale extent hides a "
                      "stale box.", notes)
    return Result(layer, OK, [], "", notes)


def verdict(results):
    """The exit status. A run that confirmed nothing is not a clean run."""
    if any(r.status == FINDING for r in results):
        return 1
    if not results or any(r.status in (UNVERIFIED, UNKNOWN) for r in results):
        return 4
    return 0


def box_text(box):
    return "x %s..%s  y %s..%s" % tuple(number(v) for v in
                                        (box[0], box[2], box[1], box[3]))


def describe(results):
    lines = ["spidxcheck: %d layer(s)" % len(results)]
    for r in results:
        lines.append("%-10s %s" % (r.status, r.layer.name))
        for code, text in r.findings:
            lines.append("    %s: %s" % (code, text))
        if r.reason:
            lines.append("    %s" % r.reason)
        for note in r.notes:
            lines.append("    note: %s" % note)
        if r.status != OK:
            for label, box in (("index box", r.layer.box),
                               ("stored extent", r.layer.ext),
                               ("data envelope", r.layer.env)):
                if box is not None:
                    lines.append("    %-13s %s" % (label, box_text(box)))
    counts = [sum(1 for r in results if r.status == s)
              for s in (OK, FINDING, UNVERIFIED, UNKNOWN)]
    lines.append("")
    lines.append("%d OK, %d FINDING, %d UNVERIFIED, %d UNKNOWN"
                 % tuple(counts))
    if counts[1]:
        lines.append("A finding is about speed and where a map opens. Esri "
                     "documents that a feature outside the index box is "
                     "still returned by a spatial query.")
    return lines


# ----------------------------------------------------------- the rebuild text

def ident(text):
    """Bracket-quote a SQL identifier, or None when it cannot be quoted safely."""
    if text == "" or any(ord(c) < 32 for c in text):
        return None
    return "[" + text.replace("]", "]]") + "]"


def rebuild_statement(layer, pad=DEFAULT_PAD):
    """(sql, None) for one layer, or (None, reason) when it cannot be built.

    The box comes from the data envelope only. A rebuilt index keeps the
    scheme, grid densities and cells per object the old one had, because
    guessing them would change more than the box.
    """
    if layer.env is None:
        return None, "no data envelope, so no box can be computed from the data"
    s = size(layer.env)
    if s <= 0:
        return None, "the data is one point, so no box can be computed"
    schema, _, table = layer.name.partition(".")
    names = [ident(x) for x in (schema, table, layer.index_name, layer.column)]
    if None in names:
        return None, ("the layer name must be schema.table, and the index "
                      "name and spatial column must be given, each without "
                      "control characters")
    if all(v == "" for v in layer.grids):
        if layer.scheme.upper() not in ("", "GEOMETRY_AUTO_GRID"):
            return None, "the grid densities are missing for a manual grid index"
        using, grids = "USING GEOMETRY_AUTO_GRID", ""
    else:
        levels = [GRID_NAMES.get(v) for v in layer.grids]
        if None in levels:
            return None, "the grid densities are not four values of 16, 64 or 256"
        using, grids = "USING GEOMETRY_GRID", ", GRIDS = (%s)" % ", ".join(levels)
    if not DIGITS.fullmatch(layer.cells) or not 1 <= int(layer.cells) <= 8192:
        return None, "cells per object is missing or not from 1 to 8192"
    p = pad * s
    box = (layer.env[0] - p, layer.env[1] - p, layer.env[2] + p, layer.env[3] + p)
    return ("CREATE SPATIAL INDEX %s ON %s.%s (%s) %s WITH (BOUNDING_BOX = "
            "(%s)%s, CELLS_PER_OBJECT = %s, DROP_EXISTING = ON);"
            % (names[2], names[0], names[1], names[3], using,
               ", ".join(repr(v) for v in box), grids, layer.cells)), None


def build_script(results, pad=DEFAULT_PAD):
    """(statements, skipped) for every layer whose index box has a finding."""
    statements = []
    skipped = []
    for r in results:
        if not any(code in BOX_CODES for code, _ in r.findings):
            continue
        sql, reason = rebuild_statement(r.layer, pad)
        if sql is None:
            skipped.append((r.layer.name, reason))
        else:
            statements.append("-- %s: %s\n%s" % (
                r.layer.name,
                ", ".join(code for code, _ in r.findings
                          if code in BOX_CODES), sql))
    return statements, skipped


def render_script(statements, pad):
    head = [
        "-- Written by spidxcheck. Review every line. Nothing here was run.",
        "-- Each box is the data envelope grown by %s of its larger side." % pad,
        "-- DROP_EXISTING = ON rebuilds the index under a schema "
        "modification lock.",
        "-- Spatial indexes cannot be rebuilt online, so no user can reach "
        "the table until it finishes.",
        "-- In a geodatabase, Esri documents that the box is taken from the "
        "feature class extent",
        "-- whenever the index is dropped and re-created. Recalculate the "
        "extent first, or the two",
        "-- will disagree again at the next rebuild.",
    ]
    return "\n".join(head) + "\n\n" + "\n\n".join(statements) + "\n"


# --------------------------------------------------------------------- input

def to_number(text, where):
    """A float, or None for a blank. Refuses everything float() would let by."""
    if text is None or text.strip() == "":
        return None
    text = text.strip()
    if not NUMBER.fullmatch(text):
        raise ValueError("%s: %s is not a number" % (where, ascii(text)))
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("%s: %s is not a finite number" % (where, ascii(text)))
    return value


def read_quad(rec, cols, where, strict):
    """The four numbers in cols as a box, None when all are blank."""
    vals = [to_number(rec.get(c), "%s, column %s" % (where, c)) for c in cols]
    if all(v is None for v in vals):
        return None
    if any(v is None for v in vals):
        raise ValueError("%s: %s must be all filled or all blank"
                         % (where, ", ".join(cols)))
    if strict:
        backwards = vals[0] >= vals[2] or vals[1] >= vals[3]
    else:
        backwards = vals[0] > vals[2] or vals[1] > vals[3]
    if backwards:
        raise ValueError("%s: %s is not a box, its minimum is not below its "
                         "maximum" % (where, ", ".join(cols)))
    return tuple(vals)


QUADS = (
    ("box", ("box_xmin", "box_ymin", "box_xmax", "box_ymax"), True),
    ("ext", ("ext_xmin", "ext_ymin", "ext_xmax", "ext_ymax"), False),
    ("env", ("env_xmin", "env_ymin", "env_xmax", "env_ymax"), False),
)
TEXTS = (("index_name", "index_name"), ("spatial_column", "column"),
         ("tessellation_scheme", "scheme"), ("cells_per_object", "cells"))
GRID_COLS = ("level_1_grid", "level_2_grid", "level_3_grid", "level_4_grid")


def read_layers(paths):
    """Merge one or more CSV files into layers, keyed on the layer column."""
    layers = {}
    for path in paths:
        with open(path, newline="", encoding="utf-8-sig") as handle:
            reader = csv.reader(handle)
            first = next(reader, None)
            if first is None:
                raise ValueError("%s: the file is empty" % path)
            header = [h.strip().lower() for h in first]
            if "layer" not in header:
                raise ValueError("%s: no layer column. It has: %s"
                                 % (path, ", ".join(header)))
            seen = set()
            for row in reader:
                if not row:
                    continue
                where = "%s line %d" % (path, reader.line_num)
                merge_row(layers, seen, dict(zip(header, row)), where)
    return list(layers.values())


def merge_row(layers, seen, rec, where):
    name = (rec.get("layer") or "").strip()
    if name == "":
        raise ValueError("%s: the layer name is blank" % where)
    key = name.casefold()
    if key in seen:
        raise ValueError("%s: layer %s appears twice in this file. Give one "
                         "row per layer; a layer with several spatial indexes "
                         "needs one file for each" % (where, ascii(name)))
    seen.add(key)
    if key not in layers:
        layers[key] = Layer(name)
    layer = layers[key]
    for attr, cols, strict in QUADS:
        quad = read_quad(rec, cols, where, strict)
        if quad is not None:
            if getattr(layer, attr) is not None:
                raise ValueError("%s: layer %s is given its %s in two files"
                                 % (where, ascii(name), cols[0][:3]))
            setattr(layer, attr, quad)
    for column, attr in TEXTS:
        value = (rec.get(column) or "").strip()
        if value:
            set_once(layer, attr, value, where)
    grids = tuple((rec.get(c) or "").strip() for c in GRID_COLS)
    if any(grids):
        set_once(layer, "grids", grids, where)


def set_once(layer, attr, value, where):
    current = getattr(layer, attr)
    if current not in ("", ("", "", "", "")) and current != value:
        raise ValueError("%s: layer %s has two different values for %s"
                         % (where, ascii(layer.name), attr))
    setattr(layer, attr, value)


def parse_pair(text):
    """'X,Y' as two floats."""
    parts = text.split(",")
    if len(parts) != 2:
        raise ValueError("%s is not X,Y" % ascii(text))
    x = to_number(parts[0], "--false-origin")
    y = to_number(parts[1], "--false-origin")
    if x is None or y is None:
        raise ValueError("%s is not X,Y" % ascii(text))
    return x, y


# ----------------------------------------------------------------------- REST

class RestError(Exception):
    """The server could not be read. Its text never holds the address."""


def check_rest_url(url):
    """(base, name) for a layer address, or ValueError. Never echoes the URL."""
    parts = urllib.parse.urlsplit(url)
    if (parts.scheme not in ("http", "https") or not parts.hostname
            or "?" in url or "#" in url or "@" in parts.netloc):
        raise ValueError("--rest needs an http or https layer address with no "
                         "query, fragment or user name. Put no token in it")
    path = parts.path.rstrip("/")
    segments = [s for s in path.split("/") if s]
    if not segments or not DIGITS.fullmatch(segments[-1]):
        raise ValueError("--rest needs an address that ends in a layer "
                         "number, such as .../FeatureServer/0")
    return "%s://%s%s" % (parts.scheme, parts.netloc, path), "/".join(segments[-3:])


def get_json(url, timeout):
    request = urllib.request.Request(
        url, headers={"Accept": "application/json", "User-Agent": "spidxcheck"})
    try:
        with contextlib.closing(urllib.request.urlopen(request, timeout=timeout)) as resp:
            body = resp.read(MAX_BODY + 1)
    except urllib.error.HTTPError as exc:
        exc.close()
        raise RestError("the server answered HTTP %d" % exc.code)
    except (OSError, http.client.HTTPException) as exc:
        raise RestError("could not read the server (%s)"
                        % getattr(exc, "reason", type(exc).__name__))
    if len(body) > MAX_BODY:
        raise RestError("the answer is larger than %d bytes" % MAX_BODY)
    try:
        data = json.loads(body.decode("utf-8"))
    except ValueError:
        raise RestError("the answer is not JSON")
    if not isinstance(data, dict):
        raise RestError("the answer is not a JSON object")
    if "error" in data:
        err = data["error"]
        code = err.get("code") if isinstance(err, dict) else None
        raise RestError("the service returned an error object (code %s)"
                        % (code if isinstance(code, int) else "unknown"))
    return data


def extent_of(value, what):
    """(xmin, ymin, xmax, ymax) from an Esri extent object, None when absent."""
    if value is None:
        return None
    keys = ("xmin", "ymin", "xmax", "ymax")
    if (not isinstance(value, dict)
            or not all(type(value.get(k)) in (int, float)
                       and abs(value[k]) < 1e300 for k in keys)
            or value["xmin"] > value["xmax"] or value["ymin"] > value["ymax"]):
        raise RestError("the %s is not a box" % what)
    return tuple(float(value[k]) for k in keys)


def query_url(base, where):
    return base + "/query?" + urllib.parse.urlencode(
        {"where": where, "returnExtentOnly": "true", "f": "json"})


def fetch_rest(url, timeout=DEFAULT_TIMEOUT):
    """A Layer filled from a hosted layer: its advertised extent, and the
    extent a returnExtentOnly query reports for all of its features.

    A second query that matches no feature decides whether the first answer
    can be trusted. Esri's documentation does not say whether the extent in a
    returnExtentOnly answer is computed from the matching features or read
    from a stored value. A server that returns an extent for a query that
    matches nothing is not answering from the features, so its answer is not
    used as the data envelope.
    """
    base, name = check_rest_url(url)
    layer = Layer(name)
    info = get_json(base + "?f=json", timeout)
    layer.ext = extent_of(info.get("extent"), "layer extent")
    layer.notes.append("here the stored extent is the layer's advertised "
                       "extent property. Esri's documentation does not say "
                       "whether a service reads it from the stored extent")
    if info.get("supportsReturningQueryExtent") is not True:
        layer.notes.append("the layer does not advertise "
                           "supportsReturningQueryExtent, so no data envelope "
                           "was read")
        return layer
    answer = get_json(query_url(base, "1=1"), timeout)
    layer.env = extent_of(answer.get("extent"), "query extent")
    if layer.env is None:
        layer.notes.append("the query returned no extent")
    else:
        empty = extent_of(get_json(query_url(base, "1=0"), timeout).get("extent"),
                          "probe extent")
        if empty is None:
            layer.notes.append("probe passed: a query that matches no "
                               "feature returned no extent, so the query "
                               "extent responds to the where clause")
        else:
            layer.env = None
            layer.notes.append("probe failed: a query that matches no "
                               "feature still returned an extent, so the "
                               "server may answer from a stored value. The "
                               "query extent was not used as the data "
                               "envelope")
    return layer


# ------------------------------------------------------------------------ cli

def check_options(args):
    """The first usage error in the parsed options, or None."""
    if not 1 < args.oversize_ratio < float("inf"):
        return "--oversize-ratio must be above 1"
    if not 0 <= args.tolerance < 1:
        return "--tolerance must be from 0 up to, not including, 1"
    if not 0 < args.pad <= 1:
        return "--pad must be above 0 and at most 1"
    if not 0 < args.timeout <= 3600:
        return "--timeout must be above 0 and at most 3600 seconds"
    if args.apply and not args.script_out:
        return "--apply needs --script-out"
    if args.script_out and os.path.exists(args.script_out):
        return ("%s already exists. Name a new file; this tool never "
                "overwrites" % args.script_out)
    return None


def parse(argv):
    ap = argparse.ArgumentParser(
        prog="spidxcheck.py",
        description="Compare a spatial index box, a stored extent and the "
                    "data envelope, and say where they disagree.",
        epilog="Nothing is written without --apply. Run --sql to see the "
               "export queries. A value that starts with - needs =, as in "
               "--false-origin=-5000000,-9000000.",
        allow_abbrev=False,
    )
    ap.add_argument("files", nargs="*", metavar="CSV",
                    help="one or more CSV files with a layer column, merged "
                         "on that column")
    ap.add_argument("--rest", metavar="URL",
                    help="read a hosted layer, such as .../FeatureServer/0. "
                         "No query string or token is accepted.")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                    help="seconds to wait for the server (default %g)"
                         % DEFAULT_TIMEOUT)
    ap.add_argument("--oversize-ratio", dest="oversize_ratio", type=float,
                    default=DEFAULT_OVERSIZE_RATIO,
                    help="a box is oversize above this many times the data "
                         "(default %g)" % DEFAULT_OVERSIZE_RATIO)
    ap.add_argument("--tolerance", type=float, default=DEFAULT_TOLERANCE,
                    help="slack, as a fraction of the data's larger side, "
                         "before a box counts as missing it (default %g)"
                         % DEFAULT_TOLERANCE)
    ap.add_argument("--false-origin", dest="false_origin", metavar="X,Y",
                    help="the false origin of the spatial reference, to flag "
                         "a box or extent that starts there")
    ap.add_argument("--script-out", dest="script_out", metavar="FILE",
                    help="the SQL file for the rebuild statements")
    ap.add_argument("--pad", type=float, default=DEFAULT_PAD,
                    help="how far the rebuilt box grows past the data, as a "
                         "fraction of its larger side (default %g)"
                         % DEFAULT_PAD)
    ap.add_argument("--apply", action="store_true",
                    help="write --script-out. Without this nothing is "
                         "written.")
    ap.add_argument("--sql", action="store_true",
                    help="print the export queries and exit")
    ap.add_argument("--self-test", dest="self_test", action="store_true",
                    help="run the offline assertions and exit")
    return ap.parse_args(argv)


def tolerate_odd_characters():
    """Make print() unable to fail on a character the console cannot encode."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(errors="replace")


def main(argv=None):
    """Run the CLI. An error this code did not anticipate exits 70, because 1
    is the code a scheduled job reads as 'a finding'."""
    tolerate_odd_characters()
    try:
        return run(argv)
    except Exception as exc:
        print("error: unexpected %s: %s. This is a fault in the tool, not a "
              "finding about the data." % (type(exc).__name__, exc),
              file=sys.stderr)
        traceback.print_exc()
        return 70


def run(argv):
    try:
        args = parse(sys.argv[1:] if argv is None else argv)
    except SystemExit as exc:
        # argparse exits 2 on a flag it refuses, and 2 here means a file could
        # not be read. A mistyped --ap is a usage error, so it gets 64.
        return 64 if exc.code == 2 else exc.code
    if args.self_test:
        return self_test()
    if args.sql:
        print(SQL_TEXT, end="")
        return 0
    if not args.files and not args.rest:
        print("error: name at least one CSV file or --rest. Use --self-test "
              "to verify the tool without them.", file=sys.stderr)
        return 64
    problem = check_options(args)
    origin = None
    if problem is None and args.false_origin:
        try:
            origin = parse_pair(args.false_origin)
        except ValueError as exc:
            problem = str(exc)
    if problem is not None:
        print("error: %s." % problem, file=sys.stderr)
        return 64
    try:
        layers = read_layers(args.files)
    except ValueError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 64
    except (OSError, csv.Error) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    if args.rest:
        try:
            layers.append(fetch_rest(args.rest, args.timeout))
        except ValueError as exc:
            print("error: %s." % exc, file=sys.stderr)
            return 64
        except RestError as exc:
            print("error: %s." % exc, file=sys.stderr)
            return 2
    results = [assess(l, args.oversize_ratio, args.tolerance, origin)
               for l in layers]
    for line in describe(results):
        print(line)
    if args.script_out:
        statements, skipped = build_script(results, args.pad)
        for name, reason in skipped:
            print("skipped %s: %s" % (name, reason))
        if not statements:
            print("\nNo rebuild statement to write.")
        elif args.apply:
            try:
                with open(args.script_out, "x", encoding="utf-8",
                          newline="\n") as handle:
                    handle.write(render_script(statements, args.pad))
            except OSError as exc:
                print("error: could not write %s: %s"
                      % (args.script_out, exc), file=sys.stderr)
                return 2
            print("\nwrote %d statement(s) to %s"
                  % (len(statements), args.script_out))
        else:
            print("\nCheck only. %d statement(s) for %s were not written. "
                  "Re-run with --apply." % (len(statements), args.script_out))
    return verdict(results)


# ------------------------------------------------------------------ self-test

class Harness(object):
    """Counts assertions. check() and raises() are tested on their red path."""

    def __init__(self):
        self.passed = 0
        self.failed = []

    def check(self, cond, label):
        if cond:
            self.passed += 1
            print("PASS  %s" % label)
        else:
            self.failed.append(label)
            print("FAIL  %s" % label)

    def raises(self, fn, label, kind=ValueError):
        try:
            fn()
        except kind:
            self.check(True, label)
        except Exception as exc:
            self.check(False, "%s (wrong exception %r)" % (label, exc))
        else:
            self.check(False, "%s (no error raised)" % label)

    def summary(self):
        print("-" * 68)
        total = self.passed + len(self.failed)
        if self.failed:
            print("%d assertions, %d failed" % (total, len(self.failed)))
            for label in self.failed:
                print("  FAILED: %s" % label)
            return 1
        print("%d assertions, 0 failed" % total)
        return 0


def self_test():
    """Run the assertions in a temp directory that is removed even when one of
    them raises."""
    tmp = tempfile.mkdtemp(prefix="spidxcheck-selftest-")
    try:
        return _self_test(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _self_test(tmp):
    """Assertions over the decision core and the CLI. The only socket is a
    stub HTTP server on 127.0.0.1. The only files are under tmp."""
    import http.server
    import socket
    import threading

    h = Harness()
    check = h.check
    raises = h.raises
    pinned = "  <-- pinned defect"

    # A made-up projected grid: data in a 4000 by 4000 square, a false origin
    # far to the lower left, and a box that spans a whole coordinate system.
    DATA = (1000.0, 2000.0, 5000.0, 6000.0)
    GOOD = (900.0, 1900.0, 5100.0, 6100.0)
    FO = (-5000000.0, -9000000.0)
    PLANET = (-5000000.0, -9000000.0, 5000000.0, 9000000.0)

    def layer(name, box=None, ext=None, env=None, **kw):
        item = Layer(name)
        item.box, item.ext, item.env = box, ext, env
        for key, value in kw.items():
            setattr(item, key, value)
        return item

    def codes(result):
        return [c for c, _ in result.findings]

    def go(argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def put(name, text):
        path = os.path.join(tmp, name)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        return path

    def listing():
        return sorted(os.listdir(tmp))

    print("spidxcheck self-test: temporary files and a loopback stub only, "
          "no database, no credentials")
    print("-" * 68)

    # ---- the three boxes agree
    r = assess(layer("a.t", GOOD, DATA, DATA))
    check(r.status == OK and not r.findings,
          "an index box a little larger than the data, with the extent equal "
          "to the data, is OK")
    check(verdict([r]) == 0, "a run of one OK layer exits 0")

    # ---- the disaster: a box built from an extent at the false origin
    r = assess(layer("a.t", PLANET, PLANET, DATA), origin=FO)
    check(codes(r) == ["FALSE_ORIGIN_BOX", "FALSE_ORIGIN_EXTENT"],
          "a box and an extent that start at the false origin are named "
          "as such" + pinned)
    r = assess(layer("a.t", PLANET, DATA, DATA))
    check(codes(r) == ["BOX_TOO_BIG"] and r.status == FINDING,
          "a continent-sized box over healthy data is BOX_TOO_BIG without "
          "any false origin given" + pinned)
    r = assess(layer("a.t", GOOD, PLANET, DATA))
    check(codes(r) == ["EXTENT_TOO_BIG"],
          "a continent-sized stored extent with a sound box is EXTENT_TOO_BIG, "
          "and the sound box is not blamed")
    r = assess(layer("a.t", PLANET, PLANET, DATA))
    check(codes(r) == ["BOX_TOO_BIG", "EXTENT_TOO_BIG"],
          "a box and an extent that are both oversize give both findings")
    r = assess(layer("a.t", PLANET, PLANET, DATA), origin=(7.0, 7.0))
    check(codes(r) == ["BOX_TOO_BIG", "EXTENT_TOO_BIG"],
          "a false origin that the box does not start at is not reported")
    r = assess(layer("a.t", (-4999999.5, -8999999.5, 5.0e6, 9.0e6), None, DATA),
               origin=FO)
    check(codes(r) == ["FALSE_ORIGIN_BOX"],
          "a corner within one unit of the false origin counts as on it")
    r = assess(layer("a.t", (-4999999.0, -9000000.0, 5.0e6, 9.0e6), None, DATA),
               origin=FO)
    check(codes(r) == ["FALSE_ORIGIN_BOX"],
          "a corner exactly one unit from the false origin counts as on it, "
          "the limit is inclusive")
    r = assess(layer("a.t", (-4999998.0, -9000000.0, 5.0e6, 9.0e6), None, DATA),
               origin=FO)
    check(codes(r) == ["BOX_TOO_BIG"],
          "a corner two units from the false origin does not")

    # ---- every message says slower or where a map opens, never lost rows
    r = assess(layer("a.t", (1500.0, 2000.0, 45000.0, 46000.0),
                     (1500.0, 2000.0, 5000.0, 6000.0), DATA))
    check(codes(r) == ["BOX_MISSES_DATA", "BOX_TOO_BIG", "EXTENT_CLIPS_DATA"],
          "a box that is oversize and also misses the data gives both "
          "findings, and a clipped extent a third")
    every = []
    for case in (assess(layer("a.t", PLANET, PLANET, DATA)),
                 assess(layer("a.t", PLANET, PLANET, DATA), origin=FO), r):
        every.extend(t for _, t in case.findings)
    banned = ("lost", "loss", "gone", "delet", "missing row", "drop", "corrupt")
    check(not any(b in t.lower() for t in every for b in banned),
          "no finding says rows are lost, gone, deleted or corrupt"
          + pinned)
    check(all(("slower" in t) or ("map" in t) for t in every),
          "every finding says what is slower or where a map opens")
    check("still returned" in dict(r.findings)["BOX_MISSES_DATA"],
          "a box that misses the data says the features are still returned"
          + pinned)

    # ---- tolerance and thresholds at their exact edge
    r = assess(layer("a.t", (1000.3, 2000.0, 5000.0, 6000.0), DATA, DATA))
    check(r.status == OK,
          "a box 0.3 units short of 4000 is inside the 1e-4 float noise"
          + pinned)
    r = assess(layer("a.t", (1002.0, 2000.0, 5000.0, 6000.0), DATA, DATA))
    check(codes(r) == ["BOX_MISSES_DATA"],
          "a box 2 units short is a miss, so a true miss still trips")
    r = assess(layer("a.t", (2000.0, 2000.0, 5000.0, 6000.0), DATA, DATA),
               tol=0.25)
    check(r.status == OK, "a shortfall of exactly the tolerance is inclusive")
    r = assess(layer("a.t", (2000.001, 2000.0, 5000.0, 6000.0), DATA, DATA),
               tol=0.25)
    check(codes(r) == ["BOX_MISSES_DATA"],
          "a shortfall just over the tolerance is a miss")
    r = assess(layer("a.t", (1000.0, 2000.0, 41000.0, 42000.0), DATA, DATA))
    check(r.status == OK, "a box exactly 10 times the data is not oversize")
    r = assess(layer("a.t", (1000.0, 2000.0, 41001.0, 42000.0), DATA, DATA))
    check(codes(r) == ["BOX_TOO_BIG"], "a box just over 10 times is oversize")
    r = assess(layer("a.t", (1000.0, 2000.0, 9000.0, 10000.0), DATA, DATA),
               ratio=1.5)
    check(codes(r) == ["BOX_TOO_BIG"], "the oversize ratio is a setting")
    r = assess(layer("a.t", (0.0, 0.0, 9000.0, 9000.0), DATA, DATA))
    check(r.status == OK, "a box that is merely loose is not a finding")

    # ---- data with no width, or no size at all
    line = (3000.0, 2000.0, 3000.0, 6000.0)
    r = assess(layer("a.t", (2900.0, 1900.0, 3100.0, 6100.0), line, line))
    check(r.status == OK,
          "data that is one vertical line, with no width, is judged by its "
          "length and is OK" + pinned)
    r = assess(layer("a.t", (-90000.0, 1900.0, 90000.0, 6100.0), line, line))
    check(codes(r) == ["BOX_TOO_BIG"],
          "a box far wider than a vertical line of data is oversize")
    point = (3000.0, 3000.0, 3000.0, 3000.0)
    r = assess(layer("a.t", (0.0, 0.0, 5000.0, 5000.0), point, point))
    check(r.status == OK,
          "a layer of one point is never called oversize, it has no size "
          "to be many times" + pinned)
    r = assess(layer("a.t", (0.0, 0.0, 10.0, 10.0), point, point))
    check(codes(r) == ["BOX_MISSES_DATA"],
          "a box that does not hold a layer of one point is a miss")

    # ---- a run that compared nothing is never clean
    r = assess(layer("a.t", GOOD, DATA, None))
    check(r.status == UNVERIFIED and verdict([r]) == 4,
          "a box that agrees with the stored extent, with no data envelope, "
          "is UNVERIFIED and exits 4, not 0" + pinned)
    r = assess(layer("a.t", PLANET, PLANET, None))
    check(r.status == UNVERIFIED,
          "a continent-sized box that agrees with a continent-sized extent "
          "is still UNVERIFIED, the pair cannot vouch for each other"
          + pinned)
    r = assess(layer("a.t", PLANET, DATA, None))
    check(codes(r) == ["BOX_TOO_BIG"] and "stored extent" in r.findings[0][1],
          "a box that disagrees with the stored extent is a finding that "
          "names the stored extent")
    for given, label in ((dict(env=DATA), "only a data envelope"),
                         (dict(box=GOOD), "only a box"),
                         (dict(ext=DATA), "only a stored extent"),
                         (dict(), "nothing at all")):
        r = assess(layer("a.t", **given))
        check(r.status == UNKNOWN and verdict([r]) == 4 and r.reason,
              "%s is UNKNOWN with a reason and exits 4" % label)
    check(verdict([]) == 4, "a run with no layers exits 4" + pinned)
    check(verdict([assess(layer("a.t", PLANET, DATA, DATA)),
                   assess(layer("b.t", GOOD, DATA, None))]) == 1,
          "a finding outranks an unverified layer")
    check(verdict([assess(layer("a.t", GOOD, DATA, DATA)),
                   assess(layer("b.t", GOOD, DATA, None))]) == 4,
          "one unverified layer among OK layers still exits 4")

    # ---- a geography index has no box
    r = assess(layer("a.t", GOOD, DATA, DATA, scheme="GEOGRAPHY_GRID"))
    check(r.status == OK and any("geography" in n for n in r.notes),
          "a box on a geography index is ignored with a note, and the extent "
          "is still compared")
    r = assess(layer("a.t", None, PLANET, DATA, scheme="geography_auto_grid"))
    check(codes(r) == ["EXTENT_TOO_BIG"] and not r.notes,
          "the geography test ignores case and an absent box adds no note")

    # ---- the report
    results = [assess(layer("dbo.ok", GOOD, DATA, DATA)),
               assess(layer("dbo.bad", PLANET, PLANET, DATA)),
               assess(layer("dbo.thin", GOOD, DATA, None)),
               assess(layer("dbo.none"))]
    text = "\n".join(describe(results))
    check("OK         dbo.ok" in text and "FINDING    dbo.bad" in text
          and "UNVERIFIED dbo.thin" in text and "UNKNOWN    dbo.none" in text,
          "the report puts the status before each layer name")
    check("1 OK, 1 FINDING, 1 UNVERIFIED, 1 UNKNOWN" in text,
          "the report counts each status")
    check("index box" in text.split("dbo.bad")[1].split("UNVERIFIED")[0]
          and "data envelope" in text,
          "a layer that is not OK shows the boxes it was judged on")
    check("x 900" not in text.split("dbo.ok")[1].split("FINDING")[0],
          "an OK layer shows no boxes")
    check("speed and where a map opens" in text,
          "a report with a finding says the finding is about speed")
    check("speed and where" not in "\n".join(describe(results[:1])),
          "a report with no finding does not say it")

    # ---- the rebuild statement
    base = dict(env=DATA, index_name="ix_roads", column="shape",
                scheme="GEOMETRY_GRID", grids=("16", "64", "64", "256"),
                cells="16")
    sql, why = rebuild_statement(layer("dbo.roads", **base), pad=0.25)
    check(sql == "CREATE SPATIAL INDEX [ix_roads] ON [dbo].[roads] ([shape]) "
                 "USING GEOMETRY_GRID WITH (BOUNDING_BOX = (0.0, 1000.0, "
                 "6000.0, 7000.0), GRIDS = (LOW, MEDIUM, MEDIUM, HIGH), "
                 "CELLS_PER_OBJECT = 16, DROP_EXISTING = ON);" and why is None,
          "a manual grid index is rebuilt with its own grids and cells, and "
          "a box that is the data grown by the pad")
    auto = dict(base, scheme="", grids=("", "", "", ""), cells="8")
    sql, why = rebuild_statement(layer("dbo.roads", **auto), pad=0.25)
    check(sql.startswith("CREATE SPATIAL INDEX [ix_roads] ON [dbo].[roads] "
                         "([shape]) USING GEOMETRY_AUTO_GRID WITH "
                         "(BOUNDING_BOX = (0.0,") and "GRIDS" not in sql
          and sql.endswith("CELLS_PER_OBJECT = 8, DROP_EXISTING = ON);"),
          "an auto grid index is rebuilt as auto grid, with no GRIDS clause")
    sql, _ = rebuild_statement(layer("dbo.roads", **dict(
        auto, scheme="GEOMETRY_AUTO_GRID")))
    check(sql is not None and "GEOMETRY_AUTO_GRID" in sql,
          "a scheme named GEOMETRY_AUTO_GRID with no grids is auto grid")
    sql, _ = rebuild_statement(layer("dbo.roads", **base))
    box = sql.split("BOUNDING_BOX = (")[1].split(")")[0].split(", ")
    check(float(box[0]) < 1000.0 and float(box[2]) > 5000.0
          and float(box[1]) < 2000.0 and float(box[3]) > 6000.0,
          "the default pad puts every side of the data inside the new box")
    sql, _ = rebuild_statement(layer("dbo.line", **dict(
        base, env=(3000.0, 2000.0, 3000.0, 6000.0))), pad=0.25)
    check("(2000.0, 1000.0, 4000.0, 7000.0)" in sql,
          "data with no width still gets a box wider than zero, because the "
          "pad is a fraction of the larger side" + pinned)
    sql, _ = rebuild_statement(layer("dbo.t", **dict(
        base, index_name="a]b", column="x]; DROP TABLE t;--")))
    check("[a]]b]" in sql and "([x]]; DROP TABLE t;--])" in sql,
          "a closing bracket in a name is doubled, so a name cannot end its "
          "own brackets and add a statement" + pinned)
    expect = {
        "no data envelope": dict(base, env=None),
        "one point": dict(base, env=point),
        "control character": dict(base, index_name="a\nb"),
        "no index name": dict(base, index_name=""),
        "no column": dict(base, column=""),
        "manual scheme without grids": dict(base, grids=("", "", "", "")),
        "a grid of 32": dict(base, grids=("16", "32", "64", "256")),
        "a partial grid": dict(base, grids=("16", "", "64", "256")),
        "no cells": dict(base, cells=""),
        "cells of 0": dict(base, cells="0"),
        "cells of 8193": dict(base, cells="8193"),
        "cells of text": dict(base, cells="many"),
    }
    for label, kw in sorted(expect.items()):
        sql, why = rebuild_statement(layer("dbo.roads", **kw))
        check(sql is None and why, "a layer with %s is skipped with a reason"
              % label)
    sql, why = rebuild_statement(layer("roads", **base))
    check(sql is None and why, "a layer name with no schema is skipped")
    check(rebuild_statement(layer("d.r", **dict(base, cells="8192")))[0]
          is not None and rebuild_statement(layer("d.r", **dict(
              base, cells="1")))[0] is not None,
          "cells of 1 and of 8192 are both accepted")
    check(ident("") is None and ident("a\tb") is None
          and ident("a b") == "[a b]",
          "an identifier that is empty or holds a control character is "
          "refused and a space is allowed")

    indexed = dict((k, v) for k, v in base.items() if k != "env")
    miss = assess(layer("dbo.roads", PLANET, DATA, DATA, **indexed))
    skip = assess(layer("dbo.nodata", PLANET, DATA, None, index_name="i",
                        column="c", cells="8"))
    ext_only = assess(layer("dbo.extonly", GOOD, PLANET, DATA, **indexed))
    statements, skipped = build_script([miss, skip, ext_only])
    check(len(statements) == 1
          and statements[0].startswith("-- dbo.roads: BOX_TOO_BIG\n"),
          "only a layer with an index box finding gets a statement")
    check([n for n, _ in skipped] == ["dbo.nodata"],
          "a box finding that cannot be rebuilt is reported as skipped, and "
          "an extent-only finding is not")
    miss_data = assess(layer("a.b", (0.0, 0.0, 1.0, 1.0), None, DATA))
    check(build_script([miss_data])[1][0][0] == "a.b",
          "a layer with no index name is skipped by name")
    body = render_script(statements, 0.05)
    check(body.startswith("-- Written by spidxcheck.")
          and "Nothing here was run." in body and body.endswith(";\n")
          and "schema modification lock" in body,
          "the script says it was not run and that the rebuild takes a lock")
    check(all(ord(c) < 128 for c in body),
          "the script text is ASCII when the names are")

    # ---- reading numbers
    for text, want in (("12", 12.0), (" 1.5e3 ", 1500.0), (".5", 0.5),
                       ("5.", 5.0), ("-7", -7.0), ("+2", 2.0)):
        check(to_number(text, "x") == want, "%s reads as %r"
              % (ascii(text), want))
    check(to_number("", "x") is None and to_number("  ", "x") is None
          and to_number(None, "x") is None, "a blank or absent cell is None")
    for text in ("1,234", "1_000", "nan", "inf", "Infinity", "1e999", "0x10",
                 "12abc", "\uff11\uff12", "1 2"):
        raises(lambda t=text: to_number(t, "x"),
               "%s is refused as a number" % ascii(text)
               + (pinned if text in ("1,234", "1_000", "\uff11\uff12")
                  else ""))
    try:
        to_number("abc", "line 7, column box_xmin")
    except ValueError as exc:
        check("line 7, column box_xmin" in str(exc),
              "the refusal of a bad number names the line and column")
    row = {"a": "1", "b": "2", "c": "3", "d": "4"}
    check(read_quad(row, "abcd", "w", True) == (1.0, 2.0, 3.0, 4.0),
          "four numbers read as a box")
    check(read_quad({}, "abcd", "w", True) is None
          and read_quad({"a": " "}, "abcd", "w", True) is None,
          "four blanks, or four absent cells, are no box")
    raises(lambda: read_quad(dict(row, c=""), "abcd", "w", True),
           "three numbers of four is refused" + pinned)
    raises(lambda: read_quad(dict(row, a="3"), "abcd", "w", True),
           "an index box with no width is refused, SQL Server needs "
           "xmax above xmin")
    raises(lambda: read_quad(dict(row, b="4"), "abcd", "w", True),
           "an index box with no height is refused")
    check(read_quad(dict(row, a="3"), "abcd", "w", False) == (3.0, 2.0, 3.0, 4.0)
          and read_quad(dict(row, b="4"), "abcd", "w", False)
          == (1.0, 4.0, 3.0, 4.0),
          "an extent or envelope may have no width or no height")
    raises(lambda: read_quad(dict(row, a="9"), "abcd", "w", False),
           "an extent whose minimum is above its maximum in x is refused")
    raises(lambda: read_quad(dict(row, b="9"), "abcd", "w", False),
           "an extent whose minimum is above its maximum in y is refused")

    check(parse_pair("1,-2.5") == (1.0, -2.5), "a false origin reads as X,Y")
    for text in ("1", "1,2,3", "1,", ",2", "a,b"):
        raises(lambda t=text: parse_pair(t),
               "%s is refused as a false origin" % ascii(text))

    # ---- reading files
    boxes = put("boxes.csv", "\ufeff Layer ,Index_Name,spatial_column,"
                "tessellation_scheme,box_xmin,box_ymin,box_xmax,box_ymax,"
                "level_1_grid,level_2_grid,level_3_grid,level_4_grid,"
                "cells_per_object\n"
                "dbo.roads,ix_roads,shape,GEOMETRY_GRID,900,1900,5100,6100,"
                "16,64,64,256,16\n"
                "\n"
                "dbo.parcels,ix_parcels,shape,GEOMETRY_GRID,-5000000,"
                "-9000000,5000000,9000000,64,64,64,64,16\n"
                "dbo.hydrants,ix_hyd,shape,,900,1900,5100,6100,,,,,8\n")
    extents = put("extents.csv", "layer,ext_xmin,ext_ymin,ext_xmax,ext_ymax\n"
                  "DBO.ROADS,1000,2000,5000,6000\n"
                  "dbo.parcels,-5000000,-9000000,5000000,9000000\n"
                  "dbo.hydrants,1000,2000,5000,6000\n")
    envs = put("envs.csv", "layer,env_xmin,env_ymin,env_xmax,env_ymax\n"
               "dbo.roads,1000,2000,5000,6000\n"
               "dbo.parcels,1000,2000,5000,6000\n"
               "dbo.hydrants,1000,2000,5000,6000\n")
    layers = read_layers([boxes, extents, envs])
    check([l.name for l in layers] == ["dbo.roads", "dbo.parcels",
                                       "dbo.hydrants"],
          "three files merge on the layer column, in first-seen order, "
          "ignoring case, a byte order mark and a padded header")
    roads = layers[0]
    check(roads.box == GOOD and roads.ext == DATA and roads.env == DATA
          and roads.index_name == "ix_roads" and roads.column == "shape"
          and roads.scheme == "GEOMETRY_GRID" and roads.cells == "16"
          and roads.grids == ("16", "64", "64", "256"),
          "the box, extent, envelope and index settings all land on one layer")
    check(layers[2].grids == ("", "", "", ""), "blank grids stay blank")
    check([assess(l).status for l in layers] == [OK, FINDING, OK],
          "the merged layers are judged: one finding among three")
    again = put("again.csv", "layer,index_name\ndbo.roads,ix_roads\n")
    check(len(read_layers([boxes, again])) == 3,
          "the same index name in two files is not a conflict")

    def bad_file(name, text, label):
        path = put(name, text)
        raises(lambda: read_layers([path]), label)

    bad_file("b1.csv", "", "an empty file is refused")
    bad_file("b2.csv", "name,box_xmin\nx,1\n",
             "a file with no layer column is refused")
    bad_file("b3.csv", "layer,box_xmin\n  ,1\n", "a blank layer name is refused")
    bad_file("b4.csv", "layer,env_xmin,env_ymin,env_xmax,env_ymax\n"
             "a.t,1,2\n", "a short row with a missing box cell is refused")
    bad_file("b5.csv", "layer,index_name\nA.t,x\na.T,x\n",
             "the same layer twice in one file is refused, whatever its case")
    bad_file("b6.csv", "env_xmin,layer\n1\n",
             "a row too short to reach the layer column is a refusal, not a "
             "crash" + pinned)
    try:
        read_layers([put("bad2.csv", "layer,env_xmin,env_ymin,env_xmax,"
                         "env_ymax\na.t,1,2,3,4\n\nb.t,1,x,3,4\n")])
    except ValueError as exc:
        check("bad2.csv line 4" in str(exc) and "env_ymin" in str(exc),
              "a blank line does not move the reported line off the file"
              + pinned)
    raises(lambda: read_layers([boxes, boxes]),
           "a box given in two files is refused")
    other = put("other.csv", "layer,index_name\ndbo.roads,ix_other\n")
    raises(lambda: read_layers([boxes, other]),
           "two different index names for one layer are refused")
    other2 = put("other2.csv", "layer,level_1_grid,level_2_grid,level_3_grid,"
                 "level_4_grid\ndbo.roads,64,64,64,64\n")
    raises(lambda: read_layers([boxes, other2]),
           "two different sets of grids for one layer are refused")
    other3 = put("other3.csv", "layer,level_1_grid,level_2_grid,level_3_grid,"
                 "level_4_grid\ndbo.roads,16,64,64,256\n")
    check(len(read_layers([boxes, other3])) == 3,
          "the same grids in two files are not a conflict")
    raises(lambda: read_layers([tmp]),
           "a directory is an OSError, not a ValueError", kind=OSError)
    raises(lambda: read_layers([os.path.join(tmp, "nope.csv")]),
           "a missing file is an OSError", kind=OSError)

    # ---- the stub server, for the REST paths
    seen = []

    def box_json(box):
        return dict(zip(("xmin", "ymin", "xmax", "ymax"), box))

    def info(name, ext, supports=True):
        body = {"name": name, "supportsReturningQueryExtent": supports}
        if ext is not None:
            body["extent"] = box_json(ext)
        return body

    def answers(name, where):
        if name in ("Healthy", "Planet"):
            if where is None:
                return 200, info(name, DATA if name == "Healthy" else PLANET)
            return 200, ({"count": 0} if where == "1=0"
                         else {"count": 9, "extent": box_json(DATA)})
        if name == "Sticky":
            if where is None:
                return 200, info(name, PLANET)
            return 200, {"count": 9, "extent": box_json(PLANET)}
        if name == "NoQueryExtent":
            return 200, info(name, DATA, supports=False)
        if name == "NoSupportKey":
            return 200, {"name": name, "extent": box_json(DATA)}
        if name == "EmptyAnswer":
            return 200, (info(name, DATA) if where is None else {"count": 0})
        if name == "NoLayerExtent":
            if where is None:
                return 200, info(name, None)
            return 200, {"count": 3, "extent": box_json(DATA)}
        if name == "ErrObj":
            return 200, {"error": {"code": 400, "message": "secret-detail"}}
        if name == "ErrStr":
            return 200, {"error": "boom"}
        if name == "ErrList":
            return 200, {"error": ["x"]}
        if name == "BadExtent":
            return 200, {"extent": {"xmin": "a"}}
        if name == "Backwards":
            return 200, {"extent": {"xmin": 5, "ymin": 1, "xmax": 1,
                                    "ymax": 2}}
        if name == "Huge":
            return 200, {"extent": {"xmin": 10 ** 400, "ymin": 1, "xmax": 1,
                                    "ymax": 2}}
        if name == "BoolExtent":
            return 200, {"extent": {"xmin": True, "ymin": 0, "xmax": 2,
                                    "ymax": 2}}
        if name == "NotObject":
            return 200, [1, 2]
        if name == "Http500":
            return 500, {"x": 1}
        if name == "NotJson":
            return 200, "<html>nope</html>"
        if name == "BadBytes":
            return 200, b"\xff\xfe\xfa"
        return 404, {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.path)
            path, _, query = self.path.partition("?")
            segs = path.split("/")
            is_query = segs[-1] == "query"
            name = segs[-4] if is_query else segs[-3]
            where = None
            if is_query:
                where = urllib.parse.parse_qs(query).get("where", [None])[0]
            status, body = answers(name, where)
            if isinstance(body, (dict, list)):
                body = json.dumps(body).encode("utf-8")
            elif isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever)
    thread.daemon = True
    thread.start()
    # A system proxy must not catch loopback traffic during the test.
    saved_env = dict((k, os.environ.get(k)) for k in ("no_proxy", "NO_PROXY"))
    os.environ["no_proxy"] = os.environ["NO_PROXY"] = "127.0.0.1,localhost"
    g = globals()
    saved_max = g["MAX_BODY"]

    def url(name):
        return ("http://127.0.0.1:%d/arcgis/rest/services/%s/FeatureServer/0"
                % (port, name))

    try:
        # ---- the address check
        for good, name in (
                ("http://h/rest/services/Roads/FeatureServer/0",
                 "Roads/FeatureServer/0"),
                ("https://h:6443/a/b/c/d/MapServer/12/", "d/MapServer/12"),
                ("http://h/3", "3")):
            check(check_rest_url(good)[1] == name,
                  "%s is named %s" % (good, name))
        check(check_rest_url("http://h/a/FeatureServer/0/")[0]
              == "http://h/a/FeatureServer/0",
              "a trailing slash is dropped from the base")
        leaky = "http://user:pw@h/a/FeatureServer/0"
        for bad in ("ftp://h/a/0", "http:///a/0", "http://h/a/0?token=SECRET",
                    "http://h/a/0?", "http://h/a/0#frag", leaky,
                    "http://h/a/FeatureServer", "http://h",
                    "http://h/a/\u00b20", "http://[::1/a/0"):
            raises(lambda b=bad: check_rest_url(b),
                   "%s is refused as a layer address" % ascii(bad))
        try:
            check_rest_url("http://h/a/0?token=SECRET")
        except ValueError as exc:
            check("SECRET" not in str(exc) and "token=" not in str(exc),
                  "the refusal does not repeat the address, so a token in it "
                  "cannot reach a log" + pinned)
        try:
            check_rest_url(leaky)
        except ValueError as exc:
            check("pw" not in str(exc) and "user:" not in str(exc),
                  "nor does it repeat a user name or password" + pinned)

        # ---- the extent reader
        check(extent_of(None, "x") is None, "an absent extent is None")
        check(extent_of({"xmin": 1, "ymin": 2.5, "xmax": 3, "ymax": 4}, "x")
              == (1.0, 2.5, 3.0, 4.0), "an extent object reads as a box")
        for label, value in (
                ("a string for a box", "x"),
                ("a missing key", {"xmin": 1}),
                ("a text number", {"xmin": "1", "ymin": 1, "xmax": 2,
                                   "ymax": 2}),
                ("a boolean", {"xmin": True, "ymin": 0, "xmax": 2, "ymax": 2}),
                ("a minimum above a maximum in x",
                 {"xmin": 5, "ymin": 1, "xmax": 1, "ymax": 2}),
                ("a minimum above a maximum in y",
                 {"xmin": 1, "ymin": 5, "xmax": 2, "ymax": 2}),
                ("an integer too large for a float",
                 {"xmin": 10 ** 400, "ymin": 1, "xmax": 1, "ymax": 2}),
                ("a NaN", {"xmin": float("nan"), "ymin": 1, "xmax": 2,
                           "ymax": 2})):
            raises(lambda v=value: extent_of(v, "x"),
                   "%s is refused as an extent" % label, kind=RestError)

        # ---- end to end against the stub
        code, out, err = go(["--rest", url("Healthy")])
        check(code == 0 and "OK         Healthy/FeatureServer/0" in out,
              "a layer whose advertised and queried extents agree is OK and "
              "exits 0")
        check(seen == ["/arcgis/rest/services/Healthy/FeatureServer/0?f=json",
                       "/arcgis/rest/services/Healthy/FeatureServer/0/query?"
                       "where=1%3D1&returnExtentOnly=true&f=json",
                       "/arcgis/rest/services/Healthy/FeatureServer/0/query?"
                       "where=1%3D0&returnExtentOnly=true&f=json"],
              "it sent three requests: the layer, a returnExtentOnly query "
              "for where 1=1, and the same query for where 1=0, and nothing "
              "else, no token")
        check("probe passed" in out,
              "a server that returns no extent for a query that matches "
              "nothing passes the probe")
        check("127.0.0.1" not in out + err and str(port) not in out + err,
              "the report names the service path and never the host or port")
        code, out, err = go(["--rest", url("Planet")])
        check(code == 1 and "EXTENT_TOO_BIG" in out,
              "a layer that advertises a continent-sized extent over small "
              "data is a finding")
        code, out, err = go(["--rest", url("Sticky")])
        check(code == 4 and "probe failed" in out and "UNKNOWN" in out,
              "a server that answers an extent for a query that matches "
              "nothing is not trusted, so a layer it describes is UNKNOWN "
              "and never OK" + pinned)
        code, out, err = go(["--rest", url("NoQueryExtent")])
        check(code == 4 and "supportsReturningQueryExtent" in out,
              "a layer that does not advertise returnExtentOnly support is "
              "UNKNOWN, not guessed")
        code, out, err = go(["--rest", url("NoSupportKey")])
        check(code == 4 and "supportsReturningQueryExtent" in out,
              "a layer with no such property is treated the same way")
        code, out, err = go(["--rest", url("EmptyAnswer")])
        check(code == 4 and "returned no extent" in out,
              "a query that returns no extent leaves the envelope unknown")
        code, out, err = go(["--rest", url("NoLayerExtent")])
        check(code == 4 and "UNKNOWN" in out,
              "a layer with no advertised extent has nothing to compare")
        for name, want in (("ErrObj", "code 400"), ("ErrStr", "code unknown"),
                           ("ErrList", "code unknown")):
            code, out, err = go(["--rest", url(name)])
            check(code == 2 and want in err and out == ""
                  and "secret-detail" not in err,
                  "an error object from the service (%s) exits 2 with %s, "
                  "and its message is not echoed" % (name, want))
        for name, want in (("Http500", "HTTP 500"), ("NotJson", "not JSON"),
                           ("BadBytes", "not JSON"),
                           ("NotObject", "not a JSON object"),
                           ("BadExtent", "layer extent is not a box"),
                           ("Backwards", "layer extent is not a box"),
                           ("Huge", "layer extent is not a box"),
                           ("BoolExtent", "layer extent is not a box"),
                           ("Missing", "HTTP 404")):
            code, out, err = go(["--rest", url(name)])
            check(code == 2 and want in err,
                  "%s exits 2 and says %s" % (name, want))
        g["MAX_BODY"] = 20
        code, out, err = go(["--rest", url("Healthy")])
        g["MAX_BODY"] = saved_max
        check(code == 2 and "larger than 20 bytes" in err,
              "an answer larger than the cap is refused")
        probe_sock = socket.socket()
        probe_sock.bind(("127.0.0.1", 0))
        dead = probe_sock.getsockname()[1]
        probe_sock.close()
        code, out, err = go(["--rest", "http://127.0.0.1:%d/a/FeatureServer/0"
                             % dead, "--timeout", "5"])
        check(code == 2 and "could not read the server" in err
              and "127.0.0.1" not in err,
              "a server that refuses the connection exits 2 without "
              "repeating the address")
        code, out, err = go(["--rest", "http://127.0.0.1:%d/a/FeatureServer/0?"
                             "token=SECRET" % port])
        check(code == 64 and "SECRET" not in out + err and out == "",
              "an address with a token is refused with exit 64 and the token "
              "appears nowhere" + pinned)
        code, out, err = go(["--rest", "http://127.0.0.1:%d/a/b" % port])
        check(code == 64, "an address that does not end in a layer number "
              "exits 64")
        code, out, err = go([boxes, envs, "--rest", url("Healthy")])
        check(code == 1 and "4 layer(s)" in out,
              "CSV files and a REST layer are judged in one run")
    finally:
        g["MAX_BODY"] = saved_max
        server.shutdown()
        server.server_close()
        thread.join()
        for key in saved_env:
            os.environ.pop(key, None)
        os.environ.update(dict((k, v) for k, v in saved_env.items()
                               if v is not None))

    check(dict((k, os.environ.get(k)) for k in saved_env) == saved_env,
          "the proxy settings the test changed are put back")

    # ---- the command line
    code, out, err = go([])
    check(code == 64 and "at least one CSV" in err and out == "",
          "no input is a usage error and prints nothing on stdout")
    code, out, err = go([boxes, extents, envs])
    check(code == 1 and "FINDING    dbo.parcels" in out
          and "OK         dbo.roads" in out and "OK         dbo.hydrants" in out
          and "BOX_TOO_BIG" in out and "EXTENT_TOO_BIG" in out,
          "three CSV files give one finding among three layers and exit 1")
    code, out, err = go([boxes, extents, envs,
                         "--false-origin=-5000000,-9000000"])
    check(code == 1 and "FALSE_ORIGIN_BOX" in out
          and "FALSE_ORIGIN_EXTENT" in out,
          "--false-origin names a box and an extent that start there")
    code, out, err = go([boxes, extents])
    check(code == 4 and "UNVERIFIED" in out,
          "with no data envelope every layer is UNVERIFIED and the run exits 4")
    header_only = put("empty.csv", "layer,env_xmin\n")
    code, out, err = go([header_only])
    check(code == 4 and "0 layer(s)" in out,
          "a file with a header and no rows exits 4, not 0" + pinned)
    good_only = put("good.csv", "layer,box_xmin,box_ymin,box_xmax,box_ymax,"
                    "env_xmin,env_ymin,env_xmax,env_ymax\n"
                    "dbo.a,900,1900,5100,6100,1000,2000,5000,6000\n")
    code, out, err = go([good_only])
    check(code == 0 and "1 OK" in out, "a clean file exits 0")
    code, out, err = go([os.path.join(tmp, "nope.csv")])
    check(code == 2 and "nope.csv" in err, "a file that cannot be read exits 2")
    code, out, err = go([tmp])
    check(code == 2, "a directory in place of a file exits 2")
    code, out, err = go([put("badnum.csv", "layer,env_xmin,env_ymin,env_xmax,"
                             "env_ymax\na.t,1,2,3,1e999\n")])
    check(code == 64 and "not a finite number" in err,
          "a number that overflows is a usage error naming the problem")
    code, out, err = go(["--help"])
    check(code == 0 and "usage: spidxcheck.py" in out, "--help exits 0")

    # ---- the parser refuses a prefix of any flag. A parser that accepted
    # --self would start a nested self-test, so self_test is stubbed here and
    # such a parser fails this block instead of hanging.
    saved_st = g["self_test"]
    g["self_test"] = lambda: 99
    for flag, full in (("--ap", "--apply"), ("--scri", "--script-out"),
                       ("--false", "--false-origin"),
                       ("--tol", "--tolerance"), ("--over", "--oversize-ratio"),
                       ("--self", "--self-test"), ("--sq", "--sql"),
                       ("--re", "--rest"), ("--pa", "--pad"),
                       ("--time", "--timeout")):
        before = listing()
        code, out, err = go([good_only, flag])
        check(code == 64 and listing() == before,
              "%s, a prefix of %s, is refused with exit 64 and nothing is "
              "written%s" % (flag, full, pinned if flag == "--ap" else ""))
    code, out, err = go([good_only, "--ap=x"])
    g["self_test"] = saved_st
    check(code == 64, "--ap=x is refused as well")

    # ---- option checks
    for argv, label in (
            ([good_only, "--oversize-ratio", "1"], "an oversize ratio of 1"),
            ([good_only, "--oversize-ratio", "nan"], "a NaN oversize ratio"),
            ([good_only, "--oversize-ratio", "inf"], "an infinite ratio"),
            ([good_only, "--tolerance", "1"], "a tolerance of 1"),
            ([good_only, "--tolerance", "-0.1"], "a negative tolerance"),
            ([good_only, "--pad", "0"], "a pad of 0"),
            ([good_only, "--pad", "1.5"], "a pad above 1"),
            ([good_only, "--timeout", "0"], "a timeout of 0"),
            ([good_only, "--timeout", "4000"], "a timeout over an hour"),
            ([good_only, "--apply"], "--apply without --script-out"),
            ([good_only, "--false-origin", "1"], "a false origin of one number"),
            ([good_only, "--false-origin", "a,b"], "a text false origin"),
            ([good_only, "--script-out", boxes], "a script path that exists")):
        code, out, err = go(argv)
        check(code == 64 and out == "" and err.startswith("error:"),
              "%s is a usage error with nothing on stdout" % label)
    code, out, err = go([good_only, "--false-origin", "-5000000,-9000000"])
    check(code == 64 and "expected one argument" in err,
          "a false origin that starts with - needs =, and the space form is "
          "a usage error rather than a silent skip")
    check(go([good_only, "--tolerance", "0"])[0] == 0
          and go([good_only, "--pad", "1"])[0] == 0,
          "a tolerance of 0 and a pad of 1 are the accepted extremes")

    # ---- only --apply writes, and only the script
    out_sql = os.path.join(tmp, "rebuild.sql")
    before = listing()
    code, out, err = go([boxes, extents, envs, "--script-out", out_sql])
    check(code == 1 and "Check only. 1 statement(s)" in out
          and not os.path.exists(out_sql) and listing() == before,
          "--script-out without --apply writes nothing and says so"
          + pinned)
    code, out, err = go([boxes, extents, envs, "--script-out", out_sql,
                         "--apply", "--pad", "0.25"])
    with open(out_sql, encoding="utf-8") as handle:
        written = handle.read()
    check(code == 1 and "wrote 1 statement(s)" in out
          and "CREATE SPATIAL INDEX [ix_parcels] ON [dbo].[parcels]" in written
          and "GRIDS = (MEDIUM, MEDIUM, MEDIUM, MEDIUM)" in written
          and "DROP_EXISTING = ON" in written,
          "--apply writes the rebuild script, and the exit code still "
          "reports the finding")
    check(listing() == sorted(before + ["rebuild.sql"]),
          "and writes no other file")
    check(not any(w in written for w in ("DELETE", "TRUNCATE", "DROP TABLE",
                                          "UPDATE ", "INSERT")),
          "the script holds no statement that removes or changes data")
    code, out, err = go([boxes, extents, envs, "--script-out", out_sql,
                         "--apply"])
    with open(out_sql, encoding="utf-8") as handle:
        check(code == 64 and handle.read() == written,
              "a second --apply is refused with exit 64 and leaves the first "
              "script as it was" + pinned)
    code, out, err = go([boxes, extents, envs, "--script-out",
                         os.path.join(tmp, "no", "dir", "x.sql"), "--apply"])
    check(code == 2 and "could not write" in err,
          "a script path that cannot be created exits 2")
    cols = ("layer,index_name,spatial_column,cells_per_object,box_xmin,"
            "box_ymin,box_xmax,box_ymax,ext_xmin,ext_ymin,ext_xmax,ext_ymax\n")
    nodata = put("nodata.csv", cols + "dbo.q,i,c,8,-5000000,-9000000,5000000,"
                 "9000000,-5000000,-9000000,5000000,9000000\n")
    other_sql = os.path.join(tmp, "none.sql")
    code, out, err = go([nodata, "--script-out", other_sql, "--apply"])
    check(code == 4 and "No rebuild statement to write." in out
          and not os.path.exists(other_sql),
          "with nothing to rebuild no file is created")
    nodata2 = put("nodata2.csv", cols + "dbo.q,i,c,8,-5000000,-9000000,"
                  "5000000,9000000,0,0,10,10\n")
    code, out, err = go([nodata2, "--script-out", other_sql])
    check(code == 1 and "skipped dbo.q: no data envelope" in out,
          "a box finding that cannot be rebuilt is listed as skipped")

    # ---- --sql
    code, out, err = go(["--sql"])
    check(code == 0 and out == SQL_TEXT and err == "",
          "--sql prints the export queries and exits 0")
    check(all(w in out for w in ("sys.spatial_index_tessellations",
                                 "bounding_box_xmin", "EnvelopeAggregate",
                                 "STPointN", "sde.SDE_layers")),
          "the queries name the catalog view, the aggregate and the layers "
          "table")
    check(re.search(r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|CREATE|"
                    r"EXEC|MERGE|GRANT)\b", SQL_TEXT, re.I) is None,
          "the export queries hold no statement that writes")
    wanted = [c for _, cols, _ in QUADS for c in cols] + list(GRID_COLS) + [
        "layer"] + [c for c, _ in TEXTS]
    check(all(re.search(r"\b(AS )?%s\b" % c, SQL_TEXT) for c in wanted),
          "every column the CSV reader looks for is named by an export query")
    check(all(ord(c) < 128 for c in SQL_TEXT), "the export queries are ASCII")
    check(go(["--sql", "--ap"])[0] == 64, "--sql does not hide a bad flag")

    # ---- no credential reaches any stream
    secret = "S3CR3T-TOKEN-VALUE"
    code, out, err = go([good_only, "--rest", "http://127.0.0.1:1/a/0?token="
                         + secret])
    check(code == 64 and secret not in out + err,
          "a token passed in an address is in no output stream" + pinned)

    # ---- the console
    raw = io.BytesIO()
    wrapper = io.TextIOWrapper(raw, encoding="cp1252", errors="strict",
                               newline="\n")
    odd = put("odd.csv", "layer,env_xmin,env_ymin,env_xmax,env_ymax\n"
              "caf\u00e9\u4e2d.t,1,2,3,4\n")
    with contextlib.redirect_stdout(wrapper), contextlib.redirect_stderr(
            io.StringIO()):
        code = main([odd])
    wrapper.flush()
    check(code == 4 and b"UNKNOWN" in raw.getvalue()
          and b"?" in raw.getvalue(),
          "a layer name the console cannot encode prints as ? and the "
          "verdict still arrives" + pinned)

    # ---- the safety nets
    def boom(argv):
        raise RuntimeError("probe")

    saved_run = g["run"]
    g["run"] = boom
    try:
        code, out, err = go([good_only])
    finally:
        g["run"] = saved_run
    check(code == 70 and "unexpected RuntimeError" in err
          and "not a finding" in err,
          "an unexpected exception exits 70, not the 1 that means a finding"
          + pinned)

    g["self_test"] = lambda: 7
    try:
        code = main(["--self-test"])
    finally:
        g["self_test"] = saved_st
    check(code == 7,
          "main returns the exit status of --self-test, so a red self-test "
          "fails CI" + pinned)

    probe = Harness()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        probe.check(False, "probe one")
        probe.raises(lambda: None, "probe two")
        probe.raises(lambda: 1 / 0, "probe three")
        probe.raises(lambda: int("x"), "probe four")
        probe.check(True, "probe five")
        red = probe.summary()
    printed = buf.getvalue()
    check(red == 1 and probe.passed == 2 and len(probe.failed) == 3
          and "no error raised" in printed and "wrong exception" in printed
          and "5 assertions, 3 failed" in printed
          and "  FAILED: probe one" in printed,
          "check() and raises() record a failure, name it, and the footer "
          "reports failures by count and by name" + pinned)
    probe = Harness()
    with contextlib.redirect_stdout(io.StringIO()):
        probe.raises(lambda: to_number("x", "w"), "accepts the ValueError")
        probe.raises(lambda: extent_of(5, "x"), "accepts a named kind",
                     kind=RestError)
    check(probe.passed == 2 and not probe.failed,
          "and raises() accepts the exception it is looking for")

    # ---- importing the module runs nothing
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "spidxcheck_imported", os.path.abspath(__file__))
    imported = importlib.util.module_from_spec(spec)
    cap = io.StringIO()
    cache_before = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        with contextlib.redirect_stdout(cap):
            spec.loader.exec_module(imported)
    finally:
        sys.dont_write_bytecode = cache_before
    check(cap.getvalue() == "" and imported.verdict([]) == 4,
          "importing the module prints nothing and exposes the core")

    return h.summary()


if __name__ == "__main__":
    sys.exit(main())
