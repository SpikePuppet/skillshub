# Notes

Interesting behaviours worth knowing about: things that work as designed but can be surprising.

## Successful syncs don't print to the server terminal

The server leaves routine requests out of its terminal output (`Handler.log_request` in `skillhub/server.py`). A request isn't logged when:

- it returned `304 Not Modified`. Watchers get this from `/v1/bundle.zip` when the catalog hasn't changed.
- it succeeded and was for `/`, `/index.html`, `/favicon.ico`, or any `/api/` route. This covers `POST /api/sync` (the dashboard's sync button) and the dashboard's regular polling.

The dashboard and watchers poll every few seconds, and logging each check would drown out real activity. Errors (`400` and above) and other requests, such as a `200` bundle download after a catalog change, are still logged.

The background watcher (`watch` in `skillhub/cli.py`) writes to `client.log` next to the client config, not to the server terminal. It logs only when a sync changes something ("Updated N skill folders") or fails.

To see sync activity, check `client.log`, the dashboard's Logs tab, or `skillhub status` ("Last sync").
