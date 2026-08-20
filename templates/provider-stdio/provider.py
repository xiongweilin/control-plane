"""Language-neutral stdio JSONL provider — copy this directory to create a new provider."""
import json
import sys

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    request = json.loads(line)
    print(
        json.dumps(
            {
                "type": "result",
                "request_id": request["id"],
                "status": "succeeded",
                "message": request.get("instruction", ""),
                "output_artifacts": [],
                "evidence_refs": [],
            }
        ),
        flush=True,
    )
