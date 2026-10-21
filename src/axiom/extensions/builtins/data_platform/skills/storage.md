# data.storage

Storage health, measured (ADR-189): per table, bytes (table, index, total),
compressed against uncompressed and the ratio, the oldest chunk and bytes per
day; per volume, free space, bytes per day and the days of runway.

```
axi data storage                       # declared tables, every hypertable, the 10 largest others
axi data storage --top 25
axi data storage --growth-days 14      # measure the daily rate over two weeks of chunks
axi data storage --disk-path /var/lib/postgresql/data   # the volume, when not visible from here
axi data storage --json
```

For a hypertable the daily rate comes from the chunks themselves: the bytes
of the chunks covering the last `growth-days`, weighted by how much of each
falls in that window. Two rates are reported: what arrives (`raw`) and what
the volume keeps once the compression policy has run (`kept`, the raw rate
over the measured ratio). The runway uses the second. The store-health audit's
sampled database growth is used instead when it is the larger number, because
it sees what chunks cannot (a plain table filling, bloat), and the line says
which source it used.

Reads only: catalogue views and TimescaleDB's size functions, never a
`count(*)`. The report is also written to `<state>/timeseries/storage.json`
for the health surface, and the same chunk-derived rate feeds the existing
`days_to_full` disk alarm, so the alarm fires on the first run rather than
after twelve hours of samples.
