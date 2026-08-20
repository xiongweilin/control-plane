# Stdio provider template

Language-neutral stdio JSONL provider. See `examples/echo-provider/` for a working Python example.

```
my-provider/
  manifest.json
  provider.py  # reads stdin, writes stdout
```

Manifest:

```json
{
  "id": "my-stdio",
  "name": "My Stdio Provider",
  "version": "1.0.0",
  "protocol_version": "1",
  "transport": "stdio-jsonl",
  "command": ["python", "provider.py"],
  "capabilities": ["text.echo"]
}
```
