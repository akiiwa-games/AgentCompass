---
name: gemini-web
description: Run Gemini web work through Route Manager's signed-in Gemini Web Chrome (CDP 127.0.0.1:17849), including Gemini Omni 1.1 Flash (gemini-omni-1.1-flash) video generation and video-to-video editing in Google AI Studio with role-tagged source and reference media. Use when a task needs Gemini, AI Studio, Flow or Omni 1.1 through the browser rather than an API key.
---

# Gemini Web

Route Manager keeps one Google Chrome signed in to the user's Google account for Gemini work:
`<route_manager>/gemini-web/` (`gemini-web/README.md`).
The tray's **Gemini** line opens its window. This is a local tool that runs in the calling agent.
It is not a worker and launches nothing. A delegated task that uses it must run on this machine:
pass `--local-only "Gemini Web browser on this machine"` to the router.

## Check

```powershell
python -X utf8 gemini-web/omni.py status
```

`gemini_web.health` is null while the browser is stopped. `run` starts it, so a stopped browser
is fine.

## Omni 1.1 video (AI Studio)

```powershell
python -X utf8 gemini-web/omni.py run `
  --video <source.mp4> --image-ref <style1.png> --image-ref <style2.png> `
  --prompt-file <prompt.txt> --resolution 720p --aspect-ratio 16:9 --out <new run dir>
```

- **Inputs:**
  - `--video` is the clip to edit (≤10 s).
  - `--extend-video` continues a clip.
  - `--first-frame`, `--last-frame` and `--loop-frame` are keyframes.
  - `--image-ref` takes up to 6 images; `--video-ref` takes up to 3 clips of ≤3 s.
  - The prompt may refer to `<VIDEO_0>`, `<IMAGE_REF_0>` and so on; the runner prepends Omni's `[# Sources ...] [# References ...]` tags.
- **Check before sending:** run `packet` with the same arguments to see the composed prompt without sending it. Or use `run --prepare-only`, which fills AI Studio and stops before Run.
- **Results:** `<out>/state.json` and `<out>/output.mp4`. Report `conversation_url` from `state.json` as evidence of the run.
- **Duration** defaults to the source length (3–10 s); `--duration` overrides it.

When `status` is not `completed`, stop and tell the user. These are theirs to resolve:

- **`login_required`:** the user signs in once in the Gemini Web window.
- **`consent_required` and `drive_required`:** one-time AI Studio account decisions (the media acknowledgement and Enable Google Drive). Only the account owner clicks them. Never click them for the user.
- **`model_error` or `timeout_or_pending`:** the tab is left open in front. Read `final_page.png` and `aistudio_dom.json`, and retry at most once with a new `--out`.

## Other Gemini pages

Attach with `gemini_web.new_task_page(playwright, url)`. This needs `sys.path` to include the
`gemini-web` directory; use one new tab per task. Close your tab and never the browser.

```powershell
python -X utf8 gemini-web/gemini_web.py show gemini|aistudio|flow
```

gemini.google.com/videos accepts images only. Video-to-video goes through AI Studio.

## Rules

- **Transport:** this browser only. No external proxy, no API key, no other Chrome profile.
- **No system dialogs:**
  - Upload with `set_input_files` and read results inside the page.
  - Never click a download, export or "Save as" control.
  - Never collect `*.tmp` or `*.crdownload` files from Downloads.
- **Never overwrite outputs.** Give every run a new `--out`. The runner refuses a directory that already has `output.mp4`.
- **Prompts:** describe a visual style in plain terms (palette, outline, shading, resolution), not by the name of a commercial game or show, which can trip the content classifier.
