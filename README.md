# spidxcheck

Compare the box a spatial index was built on, the extent the geodatabase stored, and the envelope
of the features that are really there. Say which one is wrong and by how much, and word it as a
speed and display problem, because that is what it is.

A parcel layer is loaded once into an enterprise geodatabase, and the load never sets the extent
from the data. The stored extent sits at the false origin and reaches across the whole coordinate
system. The spatial index on the shape column is created from that extent, so its bounding box is
as large as the coordinate system allows. The parcels fill a small corner of it.

Nothing fails. Every query returns the right parcels, because the exact geometry test runs after
the index and the index only narrows the candidates. Here the index narrows nothing, so each map
click and each spatial join checks far more rows than it needs to. Every query is a little slower
than it should be, and nobody can say since when. In the case this tool came from, the service
built on the layer also reported the oversize extent as its full extent. Every map opened over
empty space, and the first thing a user did was zoom to the layer. I found no Esri page that says
a service copies the stored extent, so treat that as an observation and not a documented rule.

The arithmetic is plain. SQL Server cuts the index box into four levels of grid. Take a manual
grid index with the MEDIUM density, 8 by 8 cells, on every level. That gives 4096 cells along each
side. A box 10,000,000 units wide gives cells about 2,441
units wide. Data that is 4,000 units wide falls into two or three cells along each side, so a
query window anywhere inside the data touches the same few cells and keeps every row. This is
arithmetic from the documented grid. It is not a timing, and this tool times nothing.

The other direction is the one that gets named in the SQL Server documentation. A box that is
smaller than the data leaves some features outside it. Microsoft says the space outside the box is
a single cell numbered 0, and that only objects entirely inside the box benefit from the index.
Esri says such features are not indexed but are still returned by spatial queries. So that failure
is slow too, and not wrong.

No tool I could find lines up all three numbers. Each of the three is easy to read alone. This
tool reads them from files, compares them, and refuses to call a layer clean when it compared
nothing.

```
$ python spidxcheck.py boxes.csv extents.csv envelopes.csv --false-origin=-5000000,-9000000
spidxcheck: 3 layer(s)
OK         dbo.roads
FINDING    dbo.parcels
    FALSE_ORIGIN_BOX: the index box starts at the false origin (-5000000, -9000000). ArcGIS uses the full coordinate range of the spatial reference when no extent is set, and that range starts at the false origin. Results stay correct; queries are slower.
    FALSE_ORIGIN_EXTENT: the stored extent starts at the false origin (-5000000, -9000000), so it looks like a default that was never set from the data. A map or service that opens on the full extent opens over empty space.
    index box     x -5000000..5000000  y -9000000..9000000
    stored extent x -5000000..5000000  y -9000000..9000000
    data envelope x 1000..5000  y 2000..6000
UNVERIFIED dbo.hydrants
    the index box agrees with the stored extent, but no data envelope was given, so nothing measured the features. A stale extent hides a stale box.
    index box     x 900..5100  y 1900..6100
    stored extent x 1000..5000  y 2000..6000

1 OK, 1 FINDING, 1 UNVERIFIED, 0 UNKNOWN
A finding is about speed and where a map opens. Esri documents that a feature outside the index box is still returned by a spatial query.
```

The layers in that run are made up. The coordinates are a synthetic grid, not a real place. The
third layer shows the rule that matters most: the box and the stored extent agree, but both came
from the same catalog, and nothing looked at the features. The tool says UNVERIFIED and exits 4.

An excerpt of a real self-test run follows. The `...` lines stand for passing assertions left out
here. The count in the last line is the count of the whole run.

```
$ python spidxcheck.py --self-test
spidxcheck self-test: temporary files and a loopback stub only, no database, no credentials
--------------------------------------------------------------------
PASS  an index box a little larger than the data, with the extent equal to the data, is OK
PASS  a box and an extent that start at the false origin are named as such  <-- pinned defect
PASS  a continent-sized box over healthy data is BOX_TOO_BIG without any false origin given  <-- pinned defect
...
PASS  no finding says rows are lost, gone, deleted or corrupt  <-- pinned defect
...
PASS  a layer of one point is never called oversize, it has no size to be many times  <-- pinned defect
PASS  a box that agrees with the stored extent, with no data envelope, is UNVERIFIED and exits 4, not 0  <-- pinned defect
...
PASS  a closing bracket in a name is doubled, so a name cannot end its own brackets and add a statement  <-- pinned defect
...
PASS  a server that answers an extent for a query that matches nothing is not trusted, so a layer it describes is UNKNOWN and never OK  <-- pinned defect
PASS  an address with a token is refused with exit 64 and the token appears nowhere  <-- pinned defect
...
PASS  --ap, a prefix of --apply, is refused with exit 64 and nothing is written  <-- pinned defect
...
PASS  an unexpected exception exits 70, not the 1 that means a finding  <-- pinned defect
...
PASS  importing the module prints nothing and exposes the core
--------------------------------------------------------------------
231 assertions, 0 failed
```

## What already exists

Microsoft ships the best tool for the SQL Server half. The stored procedure
[`sys.sp_help_spatial_geometry_index`](https://learn.microsoft.com/en-us/sql/relational-databases/system-stored-procedures/sp-help-spatial-geometry-index-transact-sql)
takes one table, one index and a sample query. It reports the index bounding box, the grid sizes,
the number of objects in level 0 (outside the box), and how many rows the primary filter kept
against how many it returned. Its
[property list](https://learn.microsoft.com/en-us/sql/relational-databases/system-stored-procedures/spatial-index-stored-procedures-arguments-and-properties)
says that a high count in level 0 can hurt a query window that falls partly outside the box. It
measures a real query against the real index, which nothing here does. If you suspect one index,
run it first.

It works on one index at a time, and it needs a sample query that represents your traffic. It does
not read the extent the geodatabase stored, and it does not tell you the stored extent is the cause.
This tool is the cheap check you can run over every layer before you pick one to measure.

Esri's
[Recalculate Feature Class Extent](https://doc.esri.com/en/arcgis-pro/latest/tool-reference/data-management/recalculate-feature-class-extent.html)
tool fixes the stored extent. It does not compare the extent with anything first. Its
documentation says it needs an exclusive schema lock in an enterprise geodatabase, and that the
extent will not shrink until the database is compressed, because it covers every version.

[microsoft/SQLServerSpatialTools](https://github.com/microsoft/SQLServerSpatialTools) is a
collection of functions for the spatial types. I did not find a box comparison in its description.

On 2026-10-13 I searched GitHub (`gh search repos`) for `spidxcheck`, `spatial index sql server`,
`sql server spatial index bounding box`, `geodatabase spatial index extent`, `arcgis stored
extent`, `arcgis false origin` and `sp_help_spatial_geometry_index`. No repository compared an
index box with a stored extent and a data envelope. That shows only that the search found
nothing. It is not a claim about every tool.

## Requirements

Python 3.9 or newer and nothing else. No `arcpy`, no `pyodbc`, no third-party package, no
database driver. The tool never connects to a database. A person runs the export queries and
saves the results as CSV files. The REST mode needs a network path to the service.

The self-test prints the same 231 assertions on Windows (Python 3.13.2, 3.12.10, 3.10.20 and
3.9.25) and on Ubuntu (Python 3.12.3). The two output files are identical line for line.
`coverage run --branch spidxcheck.py --self-test` reports 100 percent of statements and branches.

```
git clone https://github.com/uhsear/spidxcheck.git
```

## Usage

Step 1. Print the export queries and run them in SSMS or `sqlcmd`.

```
python spidxcheck.py --sql
```

Step 2. Save each result as a CSV file with a header row. Use the column names the query returns.

Step 3. Name the files.

```
python spidxcheck.py boxes.csv extents.csv envelopes.csv
python spidxcheck.py boxes.csv envelopes.csv --false-origin=-5000000,-9000000
python spidxcheck.py boxes.csv extents.csv envelopes.csv --script-out rebuild.sql
python spidxcheck.py boxes.csv extents.csv envelopes.csv --script-out rebuild.sql --apply
python spidxcheck.py --rest http://host/arcgis/rest/services/Roads/FeatureServer/0
python spidxcheck.py --self-test
```

The tool merges the files on the `layer` column, ignoring case. A file may hold any of the
columns below. Blank cells mean "not given".

| Column | Meaning |
|---|---|
| `layer` | `schema.table`. Required in every file. |
| `box_xmin`, `box_ymin`, `box_xmax`, `box_ymax` | The index bounding box. Must have xmax above xmin and ymax above ymin. |
| `ext_xmin`, `ext_ymin`, `ext_xmax`, `ext_ymax` | The stored extent. |
| `env_xmin`, `env_ymin`, `env_xmax`, `env_ymax` | The envelope of the features. |
| `index_name`, `spatial_column`, `tessellation_scheme`, `cells_per_object`, `level_1_grid` to `level_4_grid` | Only needed to write the rebuild script. |

| Flag | Default | What it does |
|---|---|---|
| `CSV ...` | none | One or more files. Required unless `--rest`, `--sql` or `--self-test`. |
| `--rest URL` | off | Read one hosted layer, such as `.../FeatureServer/0`. No query string, token or user name. |
| `--timeout` | `15` | Seconds to wait for the server. Above 0 and at most 3600. |
| `--oversize-ratio` | `10` | A box is oversize above this many times the data, measured on the larger side. |
| `--tolerance` | `0.0001` | Slack, as a fraction of the data's larger side, before a box counts as missing it. |
| `--false-origin=X,Y` | off | Flag a box or extent whose lower-left corner is within 1 unit of this point. Write the `=`, because a value that starts with `-` is read as a flag. |
| `--script-out FILE` | off | Where to write the rebuild script. A dry run unless `--apply`. The file must not exist. |
| `--pad` | `0.05` | How far the rebuilt box grows past the data, as a fraction of the data's larger side. Above 0 and at most 1. |
| `--apply` | off | Actually write `--script-out`. Without it nothing is written. |
| `--sql` | off | Print the export queries and exit. |
| `--self-test` | off | Run the assertions and exit. Other flags are ignored. |

Every flag must be spelled in full. The parser sets `allow_abbrev=False`, so `--ap` is refused
with exit 64 instead of being read as `--apply`. The self-test pins that for every flag, and it
stubs the self-test while it does, so a parser that accepted `--self` would fail that block
instead of starting a nested run.

## What it checks

Each layer has up to three boxes. The tool compares the index box with the data envelope, and the
stored extent with the data envelope. If there is no envelope, it compares the index box with
the stored extent and says so.

| Code | Meaning | Why it is a speed or display problem |
|---|---|---|
| `FALSE_ORIGIN_BOX` | The index box starts at the false origin you named. | Esri: with no extent set, ArcGIS uses the full coordinate range of the spatial reference for the box. The false origin is the minimum X and Y of the domain. |
| `BOX_MISSES_DATA` | The index box does not contain the data. | Microsoft: the space outside the box is one cell, numbered 0. Esri: those features are not indexed but are still returned. Slower, not wrong. |
| `BOX_TOO_BIG` | The index box is more than 10 times the data. | The features share a few grid cells. This comes from the documented grid and is not timed. |
| `FALSE_ORIGIN_EXTENT` | The stored extent starts at the false origin. | A map or service that opens on the full extent opens over empty space. |
| `EXTENT_CLIPS_DATA` | The stored extent does not contain the data. | A map opens on part of the data. An index rebuilt from it leaves the rest outside the box. |
| `EXTENT_TOO_BIG` | The stored extent is more than 10 times the data. | Maps open over empty space. Esri says a geodatabase index box is adjusted to the latest extent when the index is dropped and re-created, so the next rebuild repeats the oversize box. |

The rules behind the codes:

- A false origin finding replaces the other two for that box. The origin explains them.
- Float noise is not a finding. The same real extent sits in two catalogs as floats that differ
  in the last digits. The default tolerance is 0.01 percent of the data's larger side, so a miss
  of a few units still trips it.
- Oversize is judged on the larger side of the data, so a layer that is one straight line, with no
  width, is not a division by zero. A layer of one point has no size, so it is never oversize.
  It can still miss.
- A geography index has no bounding box. Microsoft says the catalog columns are NULL for it and
  that a box cannot be specified. The tool ignores a box on a geography index and says so.
- No message says rows are lost, gone or deleted. The self-test checks every finding for those
  words.

The statuses:

| Status | Meaning |
|---|---|
| `OK` | Everything given was compared against the data envelope and agrees. |
| `FINDING` | At least one code above. |
| `UNVERIFIED` | The index box agrees with the stored extent, but no envelope was given. Nothing measured the features. |
| `UNKNOWN` | Too little was given to compare anything. |

## The REST mode

`--rest` reads one hosted layer with up to three requests. All three are unauthenticated GETs.

1. The layer itself with `f=json`, for its advertised `extent`.
2. A `query` with `where=1=1` and `returnExtentOnly=true`, for the extent of all its features.
3. The same query with `where=1=0`, as a probe.

Esri's query documentation says `returnExtentOnly` returns "the extent of the features that
would be returned by the query", and that it applies only when the layer's
`supportsReturningQueryExtent` is true. The parts of that page and of the layer page that I read
do not say whether the extent is computed from the matching features or read from a stored value.
So the tool does not assume. A server that computes the extent has nothing to return for a query
that matches no feature. A server that returns an extent anyway is not answering from the
features, and the tool refuses to use its answer as the data envelope. That layer is UNKNOWN, never
OK. This probe is my own test and not an Esri recommendation. I ran it only against a local stub.
A real server may answer a no-match query with an extent for a reason I do not know about, and
the layer is then UNKNOWN when it need not be.

In REST mode the "stored extent" is the layer's advertised `extent` property. The report says so,
because the documentation does not define it.

The report names a layer by the last three parts of its address, such as
`Roads/FeatureServer/0`. It never prints the host or the port.

Secured layers are out of scope. The mode sends no token, and an address with a query string, a
fragment or a user name is refused with exit 64 and is not repeated in the message. A service that
answers with an error object exits 2, and the tool prints only the numeric code, never the message.

## The rebuild script

`--script-out rebuild.sql --apply` writes one `CREATE SPATIAL INDEX ... DROP_EXISTING = ON`
statement for each layer that has an index box finding. The script is text for a person to read.
The tool never runs it and never connects to a database.

- The new box is the data envelope grown by the pad. It never comes from the stored extent.
- The scheme, the grid sizes and the cells per object come from the old index. The statement
  keeps them, because `DROP_EXISTING` cannot change the index type and a guess would change more
  than the box. A layer is skipped, with the reason printed, when any of them is missing.
- A layer is also skipped when it has no data envelope, when the data is one point, or when a name
  is empty or holds a control character.
- Names are put in brackets, and a `]` inside a name is doubled, so a name cannot end its own
  brackets and add a statement.
- The header says the rebuild takes a schema modification lock and that spatial indexes cannot be
  rebuilt online. Both are in the `CREATE SPATIAL INDEX` documentation.
- The header also says that in a geodatabase, Esri takes the box from the feature class extent
  whenever the index is dropped and re-created. Recalculate the extent first. This script fixes
  the box only.

The tool refuses a `--script-out` path that already exists, on a dry run too. It opens the file
for exclusive creation. Nothing else is ever written.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Every layer is OK. |
| 1 | At least one layer has a finding. |
| 2 | A file could not be read, the script could not be written, or the server could not be read. |
| 4 | No finding, but something could not be confirmed. A layer was UNVERIFIED or UNKNOWN, or there were no layers. |
| 64 | Usage error, including a bad number in a file and an abbreviated flag. |
| 70 | An unexpected crash. This is a fault in the tool and never a finding. |

Exit 1 is the code a scheduled job reads as "a finding". That is why a crash is 70.

## Limits

- **The SQL was never run.** `--sql` prints queries that I checked against Microsoft's
  documentation of `sys.spatial_index_tessellations`, `EnvelopeAggregate` and `STPointN`. There
  was no SQL Server instance here. Read each query before you run it. The self-test checks that
  the text holds no write statement.
- **`sde.SDE_layers` is not defined in current Esri documentation.** The current Esri page lists
  the table and gives no columns. The Esri 10.1 system-table page lists `minx`, `miny`, `maxx` and
  `maxy` without a definition. The query is marked as a suggestion to check against your release.
  The tool itself reads any CSV that has the `ext_` columns, from any source.
- **The envelope query reads every row.** Run it for one table at a time.
- **Nothing was timed.** The words "slower" and "narrows nothing" follow from the documented grid
  and from what Microsoft and Esri say about cell 0. No query was run against an oversize index,
  so I do not know how much slower yours is. Use `sp_help_spatial_geometry_index` to measure.
- **Ten times is a judgment.** A well set box is a few percent larger than the data. The
  threshold is a flag.
- **REST was run only against a loopback stub** written from the Esri documentation. No real
  ArcGIS Server or hosted service was queried. The `https` path uses the same code as `http`, and
  only `http` ran. The tool uses the system proxy settings, as `urllib` does. The self-test sets
  `no_proxy` for `127.0.0.1` while it runs and restores it.
- **One row per layer.** A layer with several spatial indexes has several rows in the box query.
  Give the tool one file per index, or it refuses the duplicate.
- **Geometry only.** A geography index has no box, so only the extent is compared for it.
- **The script file is UTF-8.** Names that are not ASCII are written as they are.
- **Every line is covered.** There are no uncovered lines to name. The coverage run uses no
  pragma and no exclusion.

## Sources

Every Esri and Microsoft behavior this tool relies on, and where it is documented.

- Microsoft, [Spatial Indexes Overview](https://learn.microsoft.com/en-us/sql/relational-databases/spatial/spatial-indexes-overview):
  the bounding box, cell 0 outside it, the four-level grid, and that only objects entirely inside
  the box benefit from the index.
- Microsoft, [sys.spatial_index_tessellations](https://learn.microsoft.com/en-us/sql/relational-databases/system-catalog-views/sys-spatial-index-tessellations-transact-sql):
  the box and grid columns, NULL for geography, NULL grid columns for the newer tessellation.
- Microsoft, [CREATE SPATIAL INDEX](https://learn.microsoft.com/en-us/sql/t-sql/statements/create-spatial-index-transact-sql):
  `BOUNDING_BOX`, `GRIDS`, `CELLS_PER_OBJECT` from 1 to 8192, `DROP_EXISTING`, no online build, the
  schema modification lock, no box for geography.
- Microsoft, [EnvelopeAggregate](https://learn.microsoft.com/en-us/sql/t-sql/spatial-geometry/envelopeaggregate-geometry-data-type)
  and [STPointN](https://learn.microsoft.com/en-us/sql/t-sql/spatial-geometry/stpointn-geometry-data-type):
  a bounding box as a geometry, NULL for no input, and NULL past the last point.
- Microsoft, [spatial index stored procedure properties](https://learn.microsoft.com/en-us/sql/relational-databases/system-stored-procedures/spatial-index-stored-procedures-arguments-and-properties).
- Esri, [SQL Server spatial types and ArcGIS](https://doc.esri.com/en/arcgis-pro/latest/help/data/geodatabases/manage-sql-server/sqlserver-spatial-types-and-arcgis.html):
  the box comes from the feature class extent, the full coordinate range is used when no extent
  is set, features outside the box are not indexed but are returned, and the box follows the latest
  extent when the index is dropped and re-created.
- Esri, [Recalculate Feature Class Extent](https://doc.esri.com/en/arcgis-pro/latest/tool-reference/data-management/recalculate-feature-class-extent.html).
- Esri, [Geodatabase system tables in SQL Server](https://doc.esri.com/en/arcgis-pro/latest/help/data/geodatabases/manage-sql-server/geodatabase-system-tables-sqlserver.html):
  do not alter the system tables except through ArcGIS software or the SDK.
- Esri, [System tables of a geodatabase in SQL Server (10.1)](https://resources.arcgis.com/en/help/main/10.1/002q/002q00000080000000.htm).
- Esri, [ISpatialReference GetFalseOriginAndUnits](https://desktop.arcgis.com/en/arcobjects/latest/net/ISpatialReference_GetFalseOriginAndUnits.htm):
  the false origin is the minimum X and Y of the XY domain.
- Esri, [Query (Feature Service/Layer)](https://developers.arcgis.com/rest/services-reference/enterprise/query-feature-service-layer/)
  and [Layer (Feature Service)](https://developers.arcgis.com/rest/services-reference/enterprise/layer-feature-service/):
  `returnExtentOnly`, `supportsReturningQueryExtent`, and the layer `extent` object.

## Where each claim was exercised

Every row names the self-test assertions, and the tool was also run by hand. The hand runs used
synthetic CSV files and a loopback stub, and are quoted above.

| Claim | Exercised by |
|---|---|
| A box and extent at the false origin are named. | The false origin assertions, the exact one-unit limit, and the `--false-origin=` run above. |
| An oversize box and extent are found, and a loose box is not. | The 10 times boundary on both sides, and the ratio flag. |
| Float noise is not a finding, a real miss is. | The 0.3 and 2 unit assertions and the inclusive tolerance edge. |
| A line, a point, a geography index. | The line, point and geography assertions. |
| A run that compared nothing is not clean. | The UNVERIFIED, UNKNOWN and no-layer assertions, all exit 4. |
| No message says rows are lost. | The banned-word assertion over every finding text. |
| Numbers are read strictly. | `1,234`, `1_000`, full-width digits, `nan`, `inf`, `1e999`, partial boxes, backwards boxes. |
| Files merge on `layer` and refuse conflicts. | Three files, a byte order mark, a blank line, a short row, duplicates, conflicting index names and grids. |
| The REST mode and its probe. | A stub server with 19 behaviors, and the three requests it recorded. |
| No token or host reaches output. | An address with a token, a user name and a password, and a search of stdout and stderr for the token. |
| Only `--apply` writes, and only the script. | A dry run that leaves the folder unchanged, an apply that adds one file, a refused second apply, and a bad path. |
| The script is safe to read. | Bracket quoting, control characters, no write statement, the lock warning. |
| Abbreviated flags are refused. | A prefix of every flag, with the self-test stubbed. |
| Exit 70 for a crash and the self-test status. | A forced exception and a stubbed self-test. |
| The harness reports a failure. | A probe harness fed failing cases. |

Three things were found by running the tool, not by reading it. Without the probe, a server that
returned the stored extent for every query read OK, so the probe now always runs. A false origin
that starts with `-` was read as a flag, so the usage text and the table show the `=` form. And a
mutation run showed that the one-unit false origin limit was not pinned, so it now is.

## Contributing

Open an issue or pull request on GitHub.

## Author

Built by [Asir Khan](https://www.linkedin.com/in/asir-khan-310317264/).

## License

MIT.

## Related

- [sdeindex](https://github.com/uhsear/sdeindex) - plan or audit an index cleanup for an enterprise
  geodatabase in SQL Server. It never drops a spatial index. This tool checks the spatial
  indexes that one leaves alone, and runs before a cleanup or after a bulk load.
