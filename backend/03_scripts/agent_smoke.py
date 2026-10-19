"""Exercise both agents and retry identity over actual HTTP, without an API key."""

import argparse
import json
from urllib.request import Request, urlopen
from uuid import uuid4


def request(base, path, body=None):
    payload = json.dumps(body).encode() if body is not None else None
    req = Request(
        base + path,
        data=payload,
        headers={"Content-Type": "application/json", "X-Pulse-Agent": "assistant-v1"},
    )
    with urlopen(req, timeout=60) as response:
        return json.load(response)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    status = request(args.base, "/api/v1/agents/status")
    assert status["mode"] == "demo", "Smoke tests must not spend a model API key"
    for example in status["examples"]:
        body = {
            "request_id": str(uuid4()),
            "agent": example["agent"],
            "question": example["question"],
            "conversation_id": None,
        }
        result = request(args.base, "/api/v1/agents/ask", body)
        assert result["status"] == "answered", result["answer"]
        assert result["evidence"]
        if example["agent"] == "investigation":
            assert result["evidence"]["status"] == "consistent"
            assert any(
                f["code"] == "api_revenue" and f["status"] == "match"
                for f in result["evidence"]["findings"]
            )
        else:
            assert result["evidence"]["sql"] and result["evidence"]["rows"]
        replay = request(args.base, "/api/v1/agents/ask", body)
        assert replay["trace"]["replayed"] is True
        assert replay["evidence"] == result["evidence"]
    print("Both agents returned database evidence; repeated request IDs replayed stored results.")


if __name__ == "__main__":
    main()
