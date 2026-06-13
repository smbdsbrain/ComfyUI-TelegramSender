# ComfyUI-TelegramSender

Custom nodes for [ComfyUI](https://github.com/comfyanonymous/ComfyUI) that send images and videos directly to Telegram chats.

This package provides four output nodes:

- **Telegram: Send Image** — sends `IMAGE` tensors as PNG to Telegram (as photo and/or document).  
  When sent as a *document*, the node preserves ComfyUI PNG metadata (including workflow), so the image can be dragged back into ComfyUI and the workflow will be restored.
- **Telegram: Send Video** — sends `VIDEO` outputs (for example, from the built-in `Create Video` node) as MP4 to Telegram (as video and/or document).
- **Telegram: Send Image Album** — collects several images into a single Telegram **album** (`sendMediaGroup`). Up to 10 optional `IMAGE` inputs.
- **Telegram: Send Video Album** — collects several videos into a single Telegram **album**. Up to 10 optional `VIDEO` inputs.

Telegram credentials (`bot_token`, `chat_id`) are **not embedded into image metadata**.

---

## Installation

1. Clone or download this repository into your ComfyUI `custom_nodes` directory, for example:

   ```text
   ComfyUI/
     custom_nodes/
       ComfyUI-TelegramSender/
         __init__.py
         web/telegramSender.js
         README.md
   ```

2. Install required Python packages (in the same environment where ComfyUI runs):

   ```bash
   pip install requests imageio imageio-ffmpeg
   ```

3. Restart ComfyUI.

After restart you should see four new nodes under category `utils/telegram`:

- `Telegram: Send Image`
- `Telegram: Send Video`
- `Telegram: Send Image Album`
- `Telegram: Send Video Album`

---

## Credentials

The nodes resolve `bot_token` and `chat_id` from the first available source, in this order:

1. **ComfyUI Settings (recommended)** — open **Settings → Telegram Sender → Credentials** and fill in:
   - **Bot token** — Telegram bot token from **@BotFather**.
   - **Chat ID / @channel** — numeric chat ID or a channel username like `@my_channel`.

   These are stored by ComfyUI in `user/default/comfy.settings.json` and read by the node at runtime.

2. **`telegram_config.json`** next to the node code (legacy, still supported):

   ```json
   {
     "bot_token": "123456789:ABCDEF_your_bot_token_here",
     "chat_id": "123456789"
   }
   ```

3. **Environment variables** — `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.

If neither source provides both values, the node raises a clear error pointing to the Settings page.

---

## Telegram: Send Image

**Type:** output node  
**Category:** `utils/telegram`  
**Name:** `Telegram: Send Image`

### Inputs

- `image` (`IMAGE`, required)  
  Batch of images (standard ComfyUI format, `B x H x W x C` float tensor in `[0, 1]`).

- `caption` (`STRING`, multiline)  
  Optional caption that will be used as the Telegram message text.  
  For batches, index is appended like `(#1)`, `(#2)` etc.

- `send_as_photo` (`BOOLEAN`, default `True`)  
  If enabled, the node sends the image via Telegram `sendPhoto`.  
  This is convenient for preview, but Telegram often recompresses photos and strips metadata.

- `send_as_document` (`BOOLEAN`, default `True`)  
  If enabled, the node sends the PNG via Telegram `sendDocument`.  
  In this mode Telegram usually does **not** re-encode the file, so the PNG keeps all ComfyUI metadata (prompt, workflow, etc.).

### Hidden inputs (handled by ComfyUI)

- `prompt` (`PROMPT`)  
- `extra_pnginfo` (`EXTRA_PNGINFO`)

The node mirrors ComfyUI's `Save Image` behavior: it embeds `prompt` and `extra_pnginfo` into PNG text chunks, unless ComfyUI was started with `--disable-metadata`.

### Behavior

- The node is an **output node** (`OUTPUT_NODE = True`) and does not pass data further — you can safely end your workflow on it.
- For image batches, each image is sent separately with an optional `(#index)` suffix in the caption.
- For maximum compatibility with ComfyUI, drag the **document PNG** from Telegram back into ComfyUI — this should restore the workflow from metadata.

---

## Telegram: Send Video

**Type:** output node  
**Category:** `utils/telegram`  
**Name:** `Telegram: Send Video`

This node is designed to work directly with the built-in **`Create Video`** node and other ComfyUI video nodes.

### Supported VIDEO formats

The node accepts the standard `VIDEO` type and tries to normalize it to one of the following:

1. **Comfy video objects** (e.g. `VideoFromComponents`, `VideoFromFile`, etc.)  
   - Must provide a `.save_to(path)` method.  
   - The node calls `video.save_to(tmp_path)` to generate a temporary `.mp4` file.

2. **Dict with path**

   ```python
   {"path": "/absolute/path/to/video.mp4"}
   ```

3. **Dict with frames and fps**

   ```python
   {
       "frames": <torch.Tensor or np.ndarray or list of frames>,
       "fps": 24
   }
   ```

   In this case the node uses `imageio` / `imageio-ffmpeg` to encode frames into a temporary MP4.

4. **String path**

   ```python
   "/absolute/path/to/video.mp4"
   ```

5. **Single-element list/tuple with string path**

   ```python
   ["./output/video.mp4"]
   ```

### Inputs

- `video` (`VIDEO`, required)  
  Connect the output of `Create Video` or any other node that produces the `VIDEO` type.

- `caption` (`STRING`, multiline)  
  Caption for the Telegram message.

- `send_as_video` (`BOOLEAN`, default `True`)  
  Sends the file via Telegram `sendVideo` (regular Telegram video with preview and streaming support).

- `send_as_document` (`BOOLEAN`, default `False`)  
  Sends the same file via `sendDocument` (as a plain file).

### Behavior

1. The node normalizes the input VIDEO using `_extract_video_spec`:
   - If a ready file path exists, it uses it directly.
   - If the input is a Comfy video object with `.save_to(path)`, it creates a temporary `.mp4` file and calls `.save_to(tmp_path)`.
   - If only frames + fps are available, it encodes a temporary `.mp4` with `imageio`.

2. The resulting `.mp4` is sent to Telegram:
   - `sendVideo` (with `supports_streaming=True`) if `send_as_video` is enabled.
   - `sendDocument` if `send_as_document` is enabled.

3. Any temporary files created by the node are removed after sending.

Like the image node, this is an **output node** and does not produce outputs.

---

## Telegram: Send Image Album

**Type:** output node  
**Category:** `utils/telegram`  
**Name:** `Telegram: Send Image Album`

Sends several images as a single Telegram **album** via `sendMediaGroup`.

### Inputs

- `image_1` … `image_10` (`IMAGE`, optional)  
  Connect one image source per input. Unconnected inputs are ignored. Each input may itself be a batch — all frames are flattened in order.
- `caption` (`STRING`, multiline) — placed on the first item of the album.
- `send_as_photo` (`BOOLEAN`, default `True`) — send the collection as a photo album.
- `send_as_document` (`BOOLEAN`, default `False`) — send the collection as a document album (PNG with ComfyUI metadata preserved).

### Behavior

- Telegram allows max **10** media per album, so larger collections are split into multiple albums automatically.
- If only one image is collected, it degrades to a single `sendPhoto` / `sendDocument`.
- Hidden inputs `prompt` / `extra_pnginfo` are embedded into the PNG (document mode), unless ComfyUI runs with `--disable-metadata`.

This replaces the need to place one `Telegram: Send Image` node per output — connect all your outputs to a single album node instead.

---

## Telegram: Send Video Album

**Type:** output node  
**Category:** `utils/telegram`  
**Name:** `Telegram: Send Video Album`

Sends several videos as a single Telegram **album** via `sendMediaGroup`.

### Inputs

- `video_1` … `video_10` (`VIDEO`, optional) — connect one video source per input.
- `caption` (`STRING`, multiline) — placed on the first item of the album.
- `send_as_video` (`BOOLEAN`, default `True`).

### Behavior

- Each video is normalized to an MP4 (same logic as `Telegram: Send Video`), then grouped into albums of up to 10.
- Temporary files are cleaned up after sending.

---

## Security & Git Tips

- Do **not** commit `telegram_config.json` to your repository.  
  Only commit `telegram_config.json.example` and keep real tokens locally.
- Telegram bot tokens should be treated as secrets. If a token leaks, revoke and regenerate it via **@BotFather**.

---

## License

Released under the [WTFPL](LICENSE) (Do What The Fuck You Want To Public License), Version 2.
