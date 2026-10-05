---
name: use-llm-broker
description: Call a local LLM (Gemma, Qwen, ...) from a lab project through the GPU broker, or free a model's VRAM. Use whenever code needs a local model.
---

# Use a local model through the broker

Every LLM in the lab goes through the GPU broker (llama-swap). Never start a model
server on a GPU yourself, and never touch GPU1 (Kaya's).

```python
from labkit.broker import LLM, running, unload

llm = LLM.for_model("qwen-impl", app="my-project")   # URL: arg, then $LAB_BROKER_URL, then 127.0.0.1:8200
text = llm.chat("You are terse.", "Summarize: ...", temperature=0.3, max_tokens=800)
```

- **Which model:** `qwen-impl` (GPU0) for any project, batch or background job. Never
  `kaya` / `kaya-llamacpp`: that is Kaya's production model, and its single request slot
  serves her WhatsApp replies, so a batch job there makes her wait. Using the model the
  coding tools already use (`qwen-impl`) also means no model swaps on GPU0.
- `app=` names your project in the User-Agent, so labwatch books its usage to it.
- From a container on the broker's docker network use `LAB_BROKER_URL=http://llm-broker:8080`.
- The first request after a load can take minutes; the default timeout is 900 s.
- Qwen thinking is off by default (`thinking=True` to allow it); `<think>` blocks are stripped.
- Which models exist and which card they use: `get("svc.llm-broker")` in the `lab` MCP.
  A new model for a new project is a broker config change: only when Kaya has been idle
  for 15 min (`~/llm-broker/bin/llm-status`), because the reload stops Kaya too.
- `running()` shows what is loaded (never loads anything). `unload(model)` frees VRAM:
  only unload your own model. Never call `/upstream/<model>/...` to look; it loads the model.
