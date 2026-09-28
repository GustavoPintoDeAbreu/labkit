---
name: send-whatsapp
description: Send a WhatsApp text or voice note from a lab project (alerts, digests, podcasts) through the lab's WAHA bridge. Use when a project needs to notify someone on WhatsApp.
---

# Send WhatsApp from a lab project

Do not write a new WAHA client. Use `labkit.waha`:

```python
from labkit.waha import MockNotifier, WahaNotifier

n = WahaNotifier(base_url=os.environ["MYAPP_WAHA_URL"], chat_id=os.environ["MYAPP_WAHA_CHAT_ID"],
                 api_key=os.environ.get("MYAPP_WAHA_API_KEY"))   # or MockNotifier() in dev/tests
n.send_text("hello")
n.send_voice_file("episode.ogg")      # OGG/Opus only
```

- Install: `labkit @ https://github.com/GustavoPintoDeAbreu/labkit/archive/refs/tags/v0.1.0.tar.gz`.
- Retries 5xx/connection errors (5, 20, 60 s) and raises at once on 4xx. Let a final
  failure fail the job (and its health ping) rather than swallowing it.
- **One WAHA only:** reuse Kaya's (`svc.kaya-waha` in the `lab` MCP; `get("svc.kaya-waha")`
  gives its address and where the API key lives). Never start a second WAHA on that session.
- On the Pi, a compose project joins the external network `pi_default` and reaches it as
  `http://kaya-waha:3000`; `labkit deploy-pi --init-env --waha-env-var MYAPP_WAHA_URL` sets that.
- Develop through `MockNotifier` (`outbox` holds what would have been sent).
- Chat ids and keys go in `.env`, never in `config.yaml`, `lab.yaml` or git.
