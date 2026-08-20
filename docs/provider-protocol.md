# External provider protocol

Protocol version 1 uses `stdio-jsonl`: one JSON request per stdin line and one
JSON result per stdout line. Diagnostics belong on stderr.

Manifest fields are documented in `examples/echo-provider/manifest.json`.
Requests contain `type`, `id`, `capability`, optional Work/Run references,
instruction, artifact references and parameters. Results contain `type`,
`request_id`, a structured status and optional message/error/artifact data.

The protocol is language-neutral; a provider may be implemented in Python,
Rust, Go, Node or another runtime.
