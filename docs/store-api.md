# Store API

`StateStore` provides typed CRUD for canonical records plus `export_state()` and
`import_state()`. `InMemoryStateStore` is intended for unit tests;
`SQLiteStateStore` is the portable local backend. Both preserve IDs and sort
records deterministically. Artifact bytes use a separate `ArtifactStore` such
as `FilesystemArtifactStore`.
