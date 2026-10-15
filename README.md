# spidxcheck

Compare the box a spatial index was built on, the extent the geodatabase stored, and the envelope
of the features that are really there. Say which one is wrong and by how much, and word it as a
speed and display problem, because that is what it is.

A parcel layer is loaded once into an enterprise geodatabase, and the load never sets the extent
from the data. The stored extent sits at the false origin and reaches across the whole coordinate
system. The spatial index on the shape column is created from that extent, so its bounding box is
as large as the coordinate system allows. The parcels fill a small corner of it.

Nothing fails. Every query returns the right parcels, because the exact geometry test runs after
the index and the index only narrows the candidates. Here the index narrows very little, so each
map click and each spatial join checks more rows than it needs to. Every query is a little slower
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

The default index is different. Microsoft says an auto grid index, the default at compatibility
level 110 and higher, has 8 levels (HLLLLLLL). That gives 262,144 cells along each side. The same
box then gives cells about 38 units wide, so the data spans about a hundred of them and the index
still narrows. The slowdown from an oversize box is likely much smaller there. The tool adds a
note to say so for an auto grid index, and it has not timed that either.

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
from the same catalog, and nothing looked at the features. The tool says UNVERIFIED. A run where
that is the only layer that is not OK exits 4. The run above exits 1, because `dbo.parcels` is a
finding.

An excerpt of a real self-test run follows. The `...` lines stand for passing assertions left out
here. The count in the last line is the count of the whole run.

```
@@EXCERPT@@
```

## What already exists

Microsoft ships the best tool for the SQL Server half. The stored procedure
[`sys.sp_help_spatial_geometry_index`](https://learn.microsoft.com/en-us/sql/relational-databases/system-stored-procedures/sp-help-spatial-geometry-index-transact-sql)
takes one table, one index and a sample query. Its
[property list](https://learn.microsoft.com/en-us/sql/relational-databases/system-stored-procedures/spatial-index-stored-procedures-arguments-and-properties)
includes the index bounding box, the grid sizes, the number of objects in level 0 (outside the
box), and how many rows the primary filter kept against how many it output. The property list
says that a sample query which falls into level 0 means the index is not appropriate for that
sample. It measures a real query against the real index, which nothing here does. If you suspect
one index, run it first.

It works on one index at a time, and it needs a sample query that represents your traffic. It does
not read the extent the geodatabase stored, and it does not tell you the stored extent is the cause.
This tool is the cheap check you can run over every layer before you pick one to measure.

Esri's
[Recalculate Feature Class Extent](https://doc.esri.com/en/arcgis-pro/latest/tool-reference/data-management/recalculate-feature-class-extent.html)
tool fixes the stored extent. Its documentation describes no comparison step before it changes the
extent. It says the tool reads all the features and updates the extent property. In an enterprise
geodatabase it needs an exclusive schema lock, and the extent covers every version, so it will not
shrink until the database is compressed.

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

The self-test prints the same @@COUNT@@ assertions on Windows (Python 3.13.2, 3.12.10, 3.10.20 and
3.9.25) and on Ubuntu (Python 3.12.3). The Windows 3.13 and Ubuntu outputs are identical line for
line once the Windows carriage returns are removed.
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
columns below. Blank cells mean "not given". The Limits section lists the file formats that
are refused.

| Column | Meaning |
|---|---|
| `layer` | `schema.table`. Required in every file. |
| `box_xmin`, `box_ymin`, `box_xmax`, `box_ymax` | The index bounding box. Must have xmax above xmin and ymax above ymin. |
| `ext_xmin`, `ext_ymin`, `ext_xmax`, `ext_ymax` | The stored extent. |
| `env_xmin`, `env_ymin`, `env_xmax`, `env_ymax` | The envelope of the features. |
| `index_name`, `spatial_column`, `tessellation_scheme`, `cells_per_object`, `level_1_grid` to `level_4_grid` | Only needed to write the rebuild script. |

| Flag | Default | What it does |
|---|---|---|
| `CSV ...` | none | One or more files. Required unless `--rest`, `--sql` or `--self-test`. A web address given here is refused with exit 64 and is not repeated. |
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

A flag the tool does not have is refused with exit 64. The message says that arguments were not
recognized and does not repeat them, because one of them could be a secret.

## What it checks

Each layer has up to three boxes. The tool compares the index box with the data envelope, and the
stored extent with the data envelope. If there is no envelope, it compares the index box with
the stored extent and says so.

| Code | Meaning | Why it is a speed or display problem |
|---|---|---|
| `FALSE_ORIGIN_BOX` | The index box starts at the false origin you named. | Esri: with no extent set, ArcGIS uses the full coordinate range of the spatial reference for the box. The false origin is the minimum X and Y of the domain. |
| `BOX_MISSES_DATA` | The index box does not contain the data. | Microsoft: the space outside the box is one cell, numbered 0. Esri: those features are not indexed but are still returned. Slower, not wrong. |
| `BOX_TOO_BIG` | The index box is more than 10 times the data. | The features share fewer grid cells than a snug box would give. This comes from the documented grid and is not timed. The effect is smaller on an auto grid index, and the tool says so. |
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
| `OK` | The index box was compared with the data envelope and agrees. For a geography index, which has no box, the stored extent agrees with the envelope. |
| `FINDING` | At least one code above. |
| `UNVERIFIED` | Something that matters was not compared. Either the index box agrees with the stored extent but no envelope was given, or the stored extent and the envelope agree but no index box was given. |
| `UNKNOWN` | Too little was given to compare anything. |

## The REST mode

`--rest` reads one hosted layer with up to three requests. All three are unauthenticated GETs.

1. The layer itself with `f=json`, for its advertised `extent` and for the
   `supportsReturningQueryExtent` flag in its `advancedQueryCapabilities`.
2. A `query` with `where=1=1` and `returnExtentOnly=true`, for the extent of all its features.
3. The same query with `where=1=0`, as a probe.

Esri's query documentation says `returnExtentOnly` returns "the extent of the features that
would be returned by the query", and that it applies only if the layer's
`supportsReturningQueryExtent` property is true. Esri's layer documentation puts that property
inside `advancedQueryCapabilities`. The first version of this tool read it from the top level of
the layer, so it never sent a query to a real service. Its test stub made the same mistake and
hid it. The stub now follows the layer page.

The pages I read do not say whether the extent is computed from the matching features or read from
a stored value, so the tool does not assume. It sends the probe. A server that returns a real
extent for a query that matches no feature is not answering from the features, and the tool
refuses to use its answer as the data envelope. That layer is UNKNOWN, never OK. The probe is my
own test and not an Esri recommendation.

On 2026-10-13 I sent the probe to Esri's public sample server, to a Census layer. It answered
with an extent whose four values were the text `NaN`. For `where=1=1` it answered with a real
extent. So that server computes the extent from the features, and a computed extent for an empty
result is NaN. The tool reads an extent whose values are all `NaN`, null or absent as no extent,
so that server passes the probe. A layer with no features gives no query extent and is UNKNOWN,
and it is not an error.

The tool does not follow a redirect. A `Location` header is data from the server, and following it
would take the verdict from a host nobody named. A redirect exits 2 with "the server answered HTTP
302". A TLS error exits 2 with a fixed phrase, because the text of that error can name the host.

In REST mode the "stored extent" is the layer's advertised `extent` property. The report says so,
because the documentation does not define it. A hosted layer shows no spatial index, so REST mode
has no index box to give. A healthy layer is UNVERIFIED and the run exits 4. A finding on the
extent still exits 1.

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
  than the box. A layer is skipped, with the reason printed, when any of them is missing. That
  includes a blank `tessellation_scheme`: the tool does not guess that a blank scheme means an
  auto grid index.
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
for exclusive creation. The rebuild script is the only file the tool writes, and only with
`--apply`. The one other thing it writes is the temporary folder that `--self-test` makes and
removes.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Every layer is OK. |
| 1 | At least one layer has a finding. A failed `--self-test` also exits 1. |
| 2 | A file could not be read, the script could not be written, or the server could not be read. |
| 4 | No finding, but something could not be confirmed. A layer was UNVERIFIED or UNKNOWN, or there were no layers. |
| 64 | Usage error, a bad number or header in a file, a file that is not UTF-8, a web address given as a file, and an abbreviated flag. |
| 70 | An unexpected crash. This is a fault in the tool and never a finding. |

Exit 1 is the code a scheduled job reads as "a finding". That is why a crash is 70.

## Limits

The first group is about the SQL and the files.

- **The SQL was never run.** `--sql` prints queries that I checked against Microsoft's
  documentation of `sys.spatial_index_tessellations`, `EnvelopeAggregate` and `STPointN`. There
  was no SQL Server instance here. Read each query before you run it. The self-test checks that
  the text holds no write statement.
- **No real catalog output was ever fed to the tool.** Microsoft's catalog page lists only
  `GEOMETRY_GRID` and `GEOGRAPHY_GRID` as values of `tessellation_scheme`. The tool also accepts
  `GEOMETRY_AUTO_GRID`, which the Spatial Indexes Overview names. If a live catalog reports an auto
  grid index as `GEOMETRY_GRID` with blank grid columns, the rebuild skips that layer.
- **`sde.SDE_layers` is only partly documented.** The current Esri page lists the table and gives no
  columns. The Esri 10.1 system-table page lists `minx`, `miny`, `maxx` and `maxy` as "Minimum
  x-coordinate value of the layer" and so on. It does not say what updates them. The query is
  marked as a suggestion to check against your release. The tool itself reads any CSV that has the
  `ext_` columns, from any source.
- **The envelope query reads every row.** Run it for one table at a time.
- **The envelope query reads the base table only.** A versioned feature class keeps edits that are
  not yet compressed in its adds and deletes tables. Run the query on the versioned view, or after a
  compress, or the envelope can be stale. Esri says the adds table of a versioned feature class has
  a spatial index of its own, so the box export lists it as a layer with a box and nothing else.
  That layer is UNKNOWN. A reader cannot tell from the report whether the envelope covers the
  current data.
- **`EnvelopeAggregate` can fail or give nothing.** Microsoft says it throws a `FormatException`
  when any geometry is invalid, and returns NULL when the SRIDs differ. The first stops the export
  query. The second gives a layer with no envelope row, which is UNVERIFIED or UNKNOWN and not a
  verdict.
- **A CSV must be UTF-8**, with or without a byte order mark. A file saved as UTF-16 (the Windows
  PowerShell 5.1 redirect default), or as ANSI with a non-ASCII name, exits 64 with only "'utf-8'
  codec can't decode byte 0xff". The message does not name the file. Save the CSV as UTF-8.
- **Only SSMS-style CSV works.** NULL must be an empty cell. A NULL written as the text NULL
  (`sqlcmd` and some grid exports) is refused as "not a number". That aborts the whole run,
  including for a geography layer whose box columns are NULL. A semicolon, a tab or a decimal comma
  is refused as well.
- **Decoration lines are not skipped.** The dashed rule that `sqlcmd` prints under the header, and
  an Excel row of bare commas, are refused with exit 64. A trailing "(n rows affected)" line is not
  refused. It becomes a layer with that name, which is UNKNOWN. Remove all three before the run.
- **Columns are matched by name and by position.** A duplicate column name in the header keeps the
  last value without a warning. Cells past the header width are ignored. An unquoted comma in a
  layer name shifts the columns, and the run is then usually refused as "not a number".
- **`--false-origin=` with an empty value is ignored.** An unset shell variable gives that. The
  false origin check is then skipped without a warning.
- **The rebuild statement keeps only the scheme, the grids, the cells and the box.** It does not
  keep the filegroup, the fill factor, compression or other index options. Review it before you
  run it.
- **The rebuild skips values written as `16.0`.** Cells and grid values that went through a
  spreadsheet or pandas as `16.0` make the layer skip. A schema name that contains a dot is split
  at the first dot. A name with a control character above 31, such as DEL or U+2028, passes the
  quoting check and is quoted as given.
- **A cells value of more than 4300 digits crashes the rebuild.** Python refuses the number, and
  the run exits 70 after the report prints. Nothing is written. No real file has that value.
- **A layer name is printed as given.** A control or escape character in a name, from a CSV or
  from a `--rest` address, reaches the console.
- **Nothing was timed.** The words "slower" and "narrows" follow from the documented grid and
  from what Microsoft and Esri say about cell 0. No query was run against an oversize index, so I
  do not know how much slower yours is. Use `sp_help_spatial_geometry_index` to measure.

The second group is about how the verdict is judged.

- **Ten times is a judgment.** A well set box is a few percent larger than the data. The
  threshold is a flag.
- **Oversize is judged on the larger side only.** A square box 100000 by 100000 over a corridor
  100000 by 100 reads OK. A layer of one point is never oversize, even under a continent-sized
  box, so it reads OK with exit 0 unless you name the false origin.
- **A layer of one point has no tolerance.** The tolerance is a fraction of the larger side, which
  is 0. Float noise of 1e-10 in a catalog-stored extent is reported as `EXTENT_CLIPS_DATA` or
  `BOX_MISSES_DATA`.
- **The false origin test ignores the data.** It matches within 1 coordinate unit, whatever the
  units, so in degrees it is 1 degree. A tight box fires `FALSE_ORIGIN_BOX` when the data itself
  starts within 1 unit of the false origin you named. A local grid with false origin 0,0 and data
  from 0.2,0.3 gives that finding and exit 1.
- **A stored extent alone at the false origin is UNKNOWN.** With no box and no envelope, the tool
  does not flag `FALSE_ORIGIN_EXTENT`, even with `--false-origin`. That check needs an envelope.
- **Without an envelope, the box is judged against the stored extent.** A correct box that is
  smaller than a stale stored extent is then reported as `BOX_MISSES_DATA`, and the text blames the
  box when the extent is the stale one.
- **One row per layer.** A layer with several spatial indexes has several rows in the box query.
  Give the tool one file per index, or it refuses the duplicate.
- **Geometry only.** A geography index has no box, so only the extent is compared for it.
- **The script file is UTF-8.** Names that are not ASCII are written as they are.

The third group is about the REST mode and the self-test.

- **REST ran against a loopback stub and once against a public sample server.** The server was an
  Esri sample server, on 2026-10-13, with one Census layer. No real enterprise server was queried.
  A real server may answer in a way the stub and that sample do not. The tool uses the system
  proxy settings, as `urllib` does. The self-test sets `no_proxy` for `127.0.0.1` while it runs and
  restores it.
- **REST mode does not compare the spatial reference.** It compares the layer extent and the query
  extent as plain numbers. If a server returns them in different coordinate systems, the verdict
  is meaningless.
- **`--timeout` applies to each socket read.** A server that sends a few bytes at a time can run
  far past it. A trial server that sent 10 bytes every 0.4 seconds held a run with `--timeout 1`
  open for about 5 seconds.
- **A bad value for a numeric flag is repeated.** `--timeout abc` prints "invalid float value:
  'abc'". Only words that no flag accepts are left out of the message.
- **The self-test opens about 40 loopback connections.** On a machine with tens of thousands of
  sockets in TIME_WAIT, the REST assertions fail until the sockets expire. The failure gives no hint
  that ports are the cause. I caused this once with 500 parallel runs.
- **The self-test and the exit code.** A failed `--self-test` exits 1, the code that also means a
  finding.
- **Every line is covered.** There are no uncovered lines to name. The coverage run uses no
  pragma and no exclusion.

## Sources

Every Esri and Microsoft behavior this tool relies on, and where it is documented. I fetched each
page on 2026-10-13 and read the passage named.

- Microsoft, [Spatial Indexes Overview](https://learn.microsoft.com/en-us/sql/relational-databases/spatial/spatial-indexes-overview):
  the bounding box, cell 0 outside it, the four-level grid, that only objects entirely inside the
  box benefit from the index, and that auto grid is the default at compatibility level 110 and
  higher, with 8 levels (HLLLLLLL) and NULL grid columns in the catalog view.
- Microsoft, [sys.spatial_index_tessellations](https://learn.microsoft.com/en-us/sql/relational-databases/system-catalog-views/sys-spatial-index-tessellations-transact-sql):
  the box and grid columns, NULL for geography, grid values 16, 64 and 256.
- Microsoft, [CREATE SPATIAL INDEX](https://learn.microsoft.com/en-us/sql/t-sql/statements/create-spatial-index-transact-sql):
  `BOUNDING_BOX`, `GRIDS`, `CELLS_PER_OBJECT` from 1 to 8192, `DROP_EXISTING`, no online build, the
  schema modification lock, no box for geography.
- Microsoft, [EnvelopeAggregate](https://learn.microsoft.com/en-us/sql/t-sql/spatial-geometry/envelopeaggregate-geometry-data-type):
  a bounding box as a geometry, a `FormatException` for invalid input, NULL for empty input or for
  different SRIDs, and NULL when every input is NULL.
- Microsoft, [STPointN](https://learn.microsoft.com/en-us/sql/t-sql/spatial-geometry/stpointn-geometry-data-type):
  NULL when the number is greater than the number of points.
- Microsoft, [spatial index stored procedure properties](https://learn.microsoft.com/en-us/sql/relational-databases/system-stored-procedures/spatial-index-stored-procedures-arguments-and-properties):
  the level 0 counts, the primary filter row counts, and the bounding box properties.
- Esri, [SQL Server spatial types and ArcGIS](https://doc.esri.com/en/arcgis-pro/latest/help/data/geodatabases/manage-sql-server/sqlserver-spatial-types-and-arcgis.html):
  the box comes from the feature class extent, the full coordinate range is used when no extent
  is set, features outside the box are not indexed but are returned, the box follows the latest
  extent when the index is dropped and re-created, and a versioned feature class has a spatial
  index on its adds table.
- Esri, [Recalculate Feature Class Extent](https://doc.esri.com/en/arcgis-pro/latest/tool-reference/data-management/recalculate-feature-class-extent.html):
  it reads all the features and updates the extent property, an enterprise extent needs an
  exclusive schema lock, and the extent covers all versions until a compress.
- Esri, [Geodatabase system tables in SQL Server](https://doc.esri.com/en/arcgis-pro/latest/help/data/geodatabases/manage-sql-server/geodatabase-system-tables-sqlserver.html):
  lists `SDE_layers`. It says to alter the system tables only through ArcGIS software or the SDK.
- Esri, [System tables of a geodatabase in SQL Server (10.1)](https://resources.arcgis.com/en/help/main/10.1/002q/002q00000080000000.htm):
  the `SDE_layers` columns `owner`, `table_name`, `minx`, `miny`, `maxx` and `maxy`.
- Esri, [ISpatialReference GetFalseOriginAndUnits](https://desktop.arcgis.com/en/arcobjects/latest/net/ISpatialReference_GetFalseOriginAndUnits.htm):
  the false origin is the minimum X and Y values of the XY domain.
- Esri, [Query (Feature Service/Layer)](https://developers.arcgis.com/rest/services-reference/enterprise/query-feature-service-layer/)
  and [Layer (Feature Service)](https://developers.arcgis.com/rest/services-reference/enterprise/layer-feature-service/):
  `returnExtentOnly`, `supportsReturningQueryExtent` inside `advancedQueryCapabilities`, and the
  layer `extent` object.

## Where each claim was exercised

Every row names the self-test assertions, and the tool was also run by hand. The hand runs used
synthetic CSV files, a loopback stub, and once an Esri public sample server.

| Claim | Exercised by |
|---|---|
| A box and extent at the false origin are named. | The false origin assertions, the exact one-unit limit on both axes, and the `--false-origin=` run above. |
| An oversize box and extent are found, and a loose box is not. | The 10 times boundary on both sides, and the ratio flag. |
| A box that misses the data is found on every edge. | The left, right, bottom, top and all-four-edge shortfall assertions. |
| Float noise is not a finding, a real miss is. | The 0.3 and 2 unit assertions and the inclusive tolerance edge. |
| A line, a point, a geography index. | The line, point and geography assertions, and one-point and one-line files. |
| A run that compared nothing is not clean. | The UNVERIFIED, UNKNOWN and no-layer assertions, all exit 4, including a layer with no index box. |
| No message says rows are lost. | The banned-word assertion over every finding text. |
| The report has a fixed shape. | One assertion on the exact lines for an OK, a FINDING, an UNVERIFIED and an UNKNOWN layer. |
| Numbers are read strictly. | `1,234`, `1_000`, full-width digits, `nan`, `inf`, `1e999`, partial boxes, backwards boxes. |
| Files merge on `layer` and refuse conflicts. | Three files, a byte order mark, a blank line, a short row, duplicates, conflicting index names and grids. |
| The REST mode and its probe. | A stub server with 22 named behaviors, the three requests it recorded, and a NaN probe answer. |
| A redirect is not followed. | A stub that answers 302, and the list of paths the stub saw. |
| No token or host reaches output. | An address with a token, a user name and a password, a certificate error that names a host, a TLS failure against a plain server, a web address given as a file, a flag with a secret value, and a search of stdout and stderr for each. |
| Only `--apply` writes, and only the script. | A dry run that leaves the folder unchanged, an apply that adds one file, a refused second apply, and a bad path. |
| The script is safe to read. | Bracket quoting, control characters, no write statement, the lock warning. |
| A blank scheme is not guessed. | A blank scheme with blank grids is skipped, an auto grid with grids is skipped, and the false origin box case gets a statement. |
| Abbreviated flags are refused. | A prefix of every flag, with the self-test stubbed. |
| Exit 70 for a crash and the self-test status. | A forced exception and a stubbed self-test. |
| The harness reports a failure. | A probe harness fed failing cases. |
| The Limits sentences. | Each was reproduced by hand on a synthetic file before it was written. |

Five things were found by running the tool or its mutants, not by reading it. Without the probe,
a server that returned the stored extent for every query read OK, so the probe now always runs. A
false origin that starts with `-` was read as a flag, so the usage text and the table show the `=`
form. A mutation run showed that the one-unit false origin limit, the top and bottom edge of the
miss test, and the exact report lines were not pinned, so they now are. A run against Esri's
sample server showed that the capability flag was read from the wrong place and that a no-match
probe answers NaN. And the first version called a layer OK when it had never seen an index box.

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
