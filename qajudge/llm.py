"""Minimal DeepSeek client (same request body as earnings_radar's client): JSON mode, thinking
off, temperature 0, one repair turn. Responses are cached in qa.sqlite by request hash, so a
rerun is free and returns the identical judgement (the judge must be reproducible)."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
import time
from typing import Callable, Optional, Tuple

Transport = Callable[[str, dict, dict], Tuple[int, dict]]


def requests_transport(timeout: float = 240.0) -> Transport:
    import requests

    s = requests.Session()

    def post(url, headers, body):
        r = s.post(url, headers=headers, json=body, timeout=timeout)
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, {"error": {"message": r.text[:300]}}

    return post


class LLMError(RuntimeError):
    pass


class LLM:
    def __init__(self, api_key: str, con: sqlite3.Connection, model: str = "deepseek-flash",
                 base_url: str = "https://api.deepseek.com", transport: Optional[Transport] = None,
                 sleep: Callable[[float], None] = time.sleep, max_tokens: int = 12000):
        key = (api_key or "").strip()
        if not key or any(ord(c) > 126 or ord(c) < 33 for c in key):
            raise LLMError("DEEPSEEK_API_KEY missing or contains non-ASCII/whitespace characters")
        self._key, self.con, self.model = key, con, model
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.transport = transport or requests_transport()
        self._sleep = sleep
        self.max_tokens = max_tokens
        self.calls = {"cached": 0, "fresh": 0}

    def _post(self, body: dict) -> dict:
        headers = {"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"}
        delay = 2.0
        for attempt in range(4):
            try:
                status, data = self.transport(self.url, headers, body)
            except Exception as exc:
                status, data = -1, {"error": {"message": str(exc)}}
            if status == 200:
                return data
            if status in (429, 500, 502, 503, -1) and attempt < 3:
                self._sleep(delay)
                delay *= 2
                continue
            raise LLMError(f"HTTP {status}: {(data.get('error') or {}).get('message', '')[:200]}")
        raise LLMError("unreachable")

    def json(self, task: str, system: str, user: str, required: list) -> dict:
        rhash = hashlib.sha256(f"{self.model}\n{system}\n{user}".encode()).hexdigest()
        r = self.con.execute("SELECT response FROM llm_cache WHERE request_hash=?", (rhash,)).fetchone()
        if r:
            self.calls["cached"] += 1
            return json.loads(r[0])
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        usage, err = {}, ""
        for _turn in range(2):
            body = {"model": self.model, "messages": messages, "response_format": {"type": "json_object"},
                    "max_tokens": self.max_tokens, "thinking": {"type": "disabled"}, "temperature": 0.0,
                    "stream": False}
            data = self._post(body)
            for k, v in (data.get("usage") or {}).items():
                if isinstance(v, int):
                    usage[k] = usage.get(k, 0) + v
            content = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
            try:
                obj = json.loads(content)
                missing = [k for k in required if k not in obj]
                if not missing:
                    self.con.execute(
                        "INSERT OR REPLACE INTO llm_cache (request_hash, model, task, created_at, response, "
                        "prompt_tokens, completion_tokens) VALUES (?,?,?,?,?,?,?)",
                        (rhash, self.model, task, dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                         json.dumps(obj, ensure_ascii=False), usage.get("prompt_tokens"), usage.get("completion_tokens")))
                    self.con.commit()
                    self.calls["fresh"] += 1
                    return obj
                err = f"missing keys {missing}"
            except ValueError:
                err = "not valid JSON"
            messages = messages + [{"role": "assistant", "content": content[:4000]},
                                   {"role": "user", "content": f"Your output failed: {err}. Return the corrected JSON object only."}]
        raise LLMError(f"{task}: {err}")
