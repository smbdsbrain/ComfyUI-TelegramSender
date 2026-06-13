import os
import io
import json
import tempfile

import numpy as np
import torch
from PIL import Image, PngImagePlugin
from comfy.cli_args import args  # чтобы уважать --disable_metadata

try:
    import requests
except ImportError:
    requests = None

try:
    import imageio.v2 as imageio
except ImportError:
    imageio = None


# ======================================================================
# ОБЩИЕ ХЕЛПЕРЫ: КРЕДЫ И ОТПРАВКА В TELEGRAM
# ======================================================================
#
# Креды (bot_token / chat_id) берём в таком порядке приоритета:
#   1) Настройки ComfyUI (Settings -> Telegram Sender) -> comfy.settings.json
#   2) telegram_config.json рядом с этим файлом (старый способ)
#   3) Переменные окружения TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID
#
# Так новым пользователям достаточно вбить значения в UI, а старые
# установки с telegram_config.json продолжают работать без изменений.


def _check_requests():
    if requests is None:
        raise RuntimeError(
            "[TelegramSender] Не установлен пакет 'requests'. "
            "Установи командой: pip install requests"
        )


def _read_settings_credentials():
    """Читаем креды из настроек ComfyUI (comfy.settings.json)."""
    try:
        import folder_paths

        settings_path = os.path.join(
            folder_paths.get_user_directory(), "default", "comfy.settings.json"
        )
    except Exception:
        return None, None

    if not os.path.isfile(settings_path):
        return None, None

    try:
        with open(settings_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None, None

    token = data.get("TelegramSender.BotToken")
    chat_id = data.get("TelegramSender.ChatId")
    return (token or None), (chat_id or None)


def _read_config_file_credentials():
    """Читаем креды из telegram_config.json рядом с нодой (старый способ)."""
    config_path = os.path.join(
        os.path.dirname(os.path.realpath(__file__)), "telegram_config.json"
    )
    if not os.path.isfile(config_path):
        return None, None

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        return None, None

    token = cfg.get("bot_token") or cfg.get("token")
    chat_id = cfg.get("chat_id")
    return (token or None), (chat_id or None)


def _resolve_credentials():
    """Возвращает (bot_token, chat_id) из первого доступного источника."""
    token, chat_id = _read_settings_credentials()

    if not token or not chat_id:
        f_token, f_chat = _read_config_file_credentials()
        token = token or f_token
        chat_id = chat_id or f_chat

    if not token:
        token = os.environ.get("TELEGRAM_BOT_TOKEN") or None
    if not chat_id:
        chat_id = os.environ.get("TELEGRAM_CHAT_ID") or None

    if not token or not chat_id:
        raise RuntimeError(
            "[TelegramSender] Не заданы bot_token и/или chat_id. Укажите их одним из способов:\n"
            "  - ComfyUI: Settings -> Telegram Sender -> Credentials (рекомендуется);\n"
            "  - файл telegram_config.json рядом с нодой;\n"
            "  - переменные окружения TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID."
        )

    _check_requests()
    return str(token).strip(), str(chat_id).strip()


def _post_telegram(method, data=None, files=None, timeout=120):
    """POST в Telegram Bot API с автоматической подстановкой chat_id."""
    token, chat_id = _resolve_credentials()
    url = f"https://api.telegram.org/bot{token}/{method}"

    payload = {"chat_id": chat_id}
    if data:
        payload.update(data)

    resp = requests.post(url, data=payload, files=files, timeout=timeout)
    resp.raise_for_status()
    return resp


_SINGLE_METHOD = {
    "photo": "sendPhoto",
    "video": "sendVideo",
    "document": "sendDocument",
}


def _send_media_group(items, media_type, caption=""):
    """
    Отправка коллекции медиа как Telegram-альбома (sendMediaGroup).

    items     — список кортежей (filename, fileobj_or_bytes, mime).
    media_type — 'photo' | 'video' | 'document'.

    Telegram ограничивает альбом 10 элементами, поэтому бьём на чанки по 10.
    Caption ставится на первый элемент первого чанка.
    Если элемент всего один — деградируем до обычной одиночной отправки.
    """
    if not items:
        return

    timeout = 600 if media_type == "video" else 120

    # Один элемент: альбом не нужен, шлём как одиночное медиа.
    if len(items) == 1:
        filename, fileobj, mime = items[0]
        data = {}
        if caption:
            data["caption"] = caption
        if media_type == "video":
            data["supports_streaming"] = True
        _post_telegram(
            _SINGLE_METHOD[media_type],
            data=data,
            files={media_type: (filename, fileobj, mime)},
            timeout=timeout,
        )
        return

    for start in range(0, len(items), 10):
        chunk = items[start : start + 10]
        media = []
        files = {}
        for idx, (filename, fileobj, mime) in enumerate(chunk):
            attach_name = f"file{start + idx}"
            entry = {"type": media_type, "media": f"attach://{attach_name}"}
            if caption and start == 0 and idx == 0:
                entry["caption"] = caption
            media.append(entry)
            files[attach_name] = (filename, fileobj, mime)

        _post_telegram(
            "sendMediaGroup",
            data={"media": json.dumps(media)},
            files=files,
            timeout=timeout,
        )


# ---------- ОБЩИЕ УТИЛИТЫ ДЛЯ ИЗОБРАЖЕНИЙ ----------

def _tensor_to_pil_list(image_tensor, node_name="TelegramSender"):
    """IMAGE (torch.Tensor BxHxWxC или HxWxC) -> список PIL.Image."""
    if not isinstance(image_tensor, torch.Tensor):
        raise TypeError(f"[{node_name}] Ожидается torch.Tensor в поле 'image'")

    if image_tensor.dim() == 3:
        batch = image_tensor.unsqueeze(0)
    else:
        batch = image_tensor

    batch = batch.clamp(0.0, 1.0).cpu().numpy()
    batch = (batch * 255.0).round().astype(np.uint8)

    pil_images = []
    for i in range(batch.shape[0]):
        arr = batch[i]
        if arr.ndim != 3:
            raise ValueError(
                f"[{node_name}] Неподдерживаемая форма изображения: {arr.shape}"
            )
        mode = "RGBA" if arr.shape[2] == 4 else "RGB"
        pil_images.append(Image.fromarray(arr, mode))

    return pil_images


def _encode_png_with_metadata(pil_image, prompt=None, extra_pnginfo=None):
    """
    Кодируем PIL.Image в PNG и вшиваем метаданные ComfyUI (prompt + extra_pnginfo),
    чтобы в PNG оказался workflow и всё остальное.
    """
    pnginfo = None

    # Уважаем глобальный флаг отключения метадаты
    if not getattr(args, "disable_metadata", False):
        pnginfo = PngImagePlugin.PngInfo()

        if prompt is not None:
            try:
                pnginfo.add_text("prompt", json.dumps(prompt, ensure_ascii=False))
            except Exception:
                pnginfo.add_text("prompt", str(prompt))

        if isinstance(extra_pnginfo, dict):
            for key, value in extra_pnginfo.items():
                try:
                    text = json.dumps(value, ensure_ascii=False)
                except Exception:
                    text = str(value)
                pnginfo.add_text(key, text)

    bio = io.BytesIO()
    if pnginfo is not None:
        pil_image.save(bio, format="PNG", pnginfo=pnginfo)
    else:
        pil_image.save(bio, format="PNG")
    bio.seek(0)
    return bio


# ---------- ОБЩИЕ УТИЛИТЫ ДЛЯ ВИДЕО ----------

def _ensure_imageio():
    if imageio is None:
        raise RuntimeError(
            "[TelegramSender] Не установлен пакет 'imageio'. "
            "Установи командой: pip install imageio imageio-ffmpeg"
        )


def _extract_video_spec(video):
    """
    Приводит VIDEO к унифицированному виду.

    Возвращает dict с одним из вариантов:
    - {"path": "...", "cleanup": False}
    - {"path": "...tmp.mp4", "cleanup": True}   # если сохраняли .save_to()
    - {"frames": frames, "fps": fps}
    """
    # 1) Просто строка — путь к файлу
    if isinstance(video, str):
        return {"path": video, "cleanup": False}

    # 2) comfy_api объекты (VideoFromComponents, VideoFromFile и т.п.)
    if hasattr(video, "save_to") and callable(getattr(video, "save_to")):
        tmp_dir = os.path.join(os.path.dirname(os.path.realpath(__file__)), "tmp")
        os.makedirs(tmp_dir, exist_ok=True)

        tmp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4", dir=tmp_dir)
        tmp_path = tmp_file.name
        tmp_file.close()

        video.save_to(tmp_path)
        return {"path": tmp_path, "cleanup": True}

    # 3) dict-структуры
    if isinstance(video, dict):
        if isinstance(video.get("path"), str):
            return {"path": video["path"], "cleanup": False}

        frames = (
            video.get("frames")
            or video.get("images")
            or video.get("video_frames")
        )
        fps = (
            video.get("fps")
            or video.get("frame_rate")
            or video.get("frame_rate_fps")
            or 25
        )
        if frames is not None:
            return {"frames": frames, "fps": fps}

    # 4) На всякий случай: ["path.mp4"]
    if isinstance(video, (list, tuple)) and len(video) == 1 and isinstance(video[0], str):
        return {"path": video[0], "cleanup": False}

    raise TypeError(
        f"[TelegramSender] Неподдерживаемый формат VIDEO: {type(video)}. "
        "Ожидался объект с .save_to(), dict со 'path' или 'frames'/'fps', либо строка-путь."
    )


def _frames_to_np_list(frames):
    """Приводит frames к списку np.ndarray HxWx3 uint8."""
    # torch.Tensor
    if isinstance(frames, torch.Tensor):
        if frames.dim() == 3:
            batch = frames.unsqueeze(0)
        else:
            batch = frames
        batch = batch.clamp(0.0, 1.0).cpu().numpy()
        batch = (batch * 255.0).round().astype(np.uint8)

        out = []
        for i in range(batch.shape[0]):
            arr = batch[i]
            if arr.ndim != 3:
                raise ValueError(
                    f"[TelegramSender] Неподдерживаемая форма кадра: {arr.shape}"
                )
            if arr.shape[2] == 4:
                arr = arr[:, :, :3]
            out.append(arr)
        return out

    # np.ndarray
    if isinstance(frames, np.ndarray):
        if frames.ndim == 3:
            batch = frames[None, ...]
        elif frames.ndim == 4:
            batch = frames
        else:
            raise ValueError(
                f"[TelegramSender] Неподдерживаемая форма np.ndarray: {frames.shape}"
            )
        if batch.dtype != np.uint8:
            batch = np.clip(batch, 0, 255).astype(np.uint8)

        out = []
        for i in range(batch.shape[0]):
            arr = batch[i]
            if arr.ndim != 3:
                raise ValueError(
                    f"[TelegramSender] Неподдерживаемая форма кадра: {arr.shape}"
                )
            if arr.shape[2] == 4:
                arr = arr[:, :, :3]
            out.append(arr)
        return out

    # list / tuple
    if isinstance(frames, (list, tuple)):
        out = []
        for f in frames:
            if isinstance(f, str):
                img = Image.open(f).convert("RGB")
                out.append(np.array(img, dtype=np.uint8))
            elif isinstance(f, Image.Image):
                out.append(np.array(f.convert("RGB"), dtype=np.uint8))
            elif isinstance(f, torch.Tensor):
                arr = f.clamp(0.0, 1.0).cpu().numpy()
                arr = (arr * 255.0).round().astype(np.uint8)
                if arr.ndim == 3 and arr.shape[0] in (1, 3, 4):
                    arr = np.moveaxis(arr, 0, 2)
                if arr.shape[2] == 4:
                    arr = arr[:, :, :3]
                out.append(arr)
            elif isinstance(f, np.ndarray):
                arr = f
                if arr.ndim == 3 and arr.shape[0] in (1, 3, 4):
                    arr = np.moveaxis(arr, 0, 2)
                if arr.dtype != np.uint8:
                    arr = np.clip(arr, 0, 255).astype(np.uint8)
                if arr.shape[2] == 4:
                    arr = arr[:, :, :3]
                out.append(arr)
            else:
                raise TypeError(
                    f"[TelegramSender] Неподдерживаемый тип кадра в списке: {type(f)}"
                )
        return out

    raise TypeError(f"[TelegramSender] Неподдерживаемый тип frames: {type(frames)}")


def _encode_video_from_frames(frames, fps):
    """Кодирует кадры в .mp4 и возвращает путь к временному файлу."""
    _ensure_imageio()

    np_frames = _frames_to_np_list(frames)
    if not np_frames:
        raise RuntimeError("[TelegramSender] Нет кадров для кодирования видео")

    tmp_dir = os.path.join(os.path.dirname(os.path.realpath(__file__)), "tmp")
    os.makedirs(tmp_dir, exist_ok=True)

    tmp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4", dir=tmp_dir)
    tmp_path = tmp_file.name
    tmp_file.close()

    writer = imageio.get_writer(tmp_path, fps=fps)
    try:
        for frame in np_frames:
            writer.append_data(frame)
    finally:
        writer.close()

    return tmp_path


def _resolve_video_to_path(video):
    """
    Приводит VIDEO к пути к mp4-файлу.
    Возвращает (path, is_temp): is_temp=True если файл временный и его надо удалить.
    """
    spec = _extract_video_spec(video)
    if "path" in spec and "frames" not in spec:
        return spec["path"], bool(spec.get("cleanup"))
    path = _encode_video_from_frames(spec["frames"], spec["fps"])
    return path, True


# ======================================================================
# НОДА: ОТПРАВКА ИЗОБРАЖЕНИЙ (PNG + workflow в метаданных)
# ======================================================================

class TelegramSendImage:
    """
    Нода для отправки изображений из ComfyUI в Telegram.

    - Берёт на вход IMAGE (batch тоже поддерживается).
    - Отправляет каждый кадр:
        * как photo (для просмотра в чате);
        * как document (PNG с метаданными, чтобы можно было
          перетащить обратно в ComfyUI и прочитать workflow).
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
                "caption": ("STRING", {"default": "", "multiline": True}),
                "send_as_photo": ("BOOLEAN", {"default": True}),
                "send_as_document": ("BOOLEAN", {"default": True}),
            },
            "hidden": {
                "prompt": "PROMPT",
                "extra_pnginfo": "EXTRA_PNGINFO",
            },
        }

    RETURN_TYPES = ()
    RETURN_NAMES = ()
    FUNCTION = "send_to_telegram"
    OUTPUT_NODE = True
    CATEGORY = "utils/telegram"

    def send_to_telegram(
        self,
        image,
        caption="",
        send_as_photo=True,
        send_as_document=True,
        prompt=None,
        extra_pnginfo=None,
    ):
        if not send_as_photo and not send_as_document:
            return ()

        pil_images = _tensor_to_pil_list(image, "TelegramSendImage")

        for idx, pil_img in enumerate(pil_images):
            if len(pil_images) > 1 and caption:
                full_caption = f"{caption} (#{idx + 1})"
            else:
                full_caption = caption

            if send_as_photo:
                bio = _encode_png_with_metadata(pil_img, prompt, extra_pnginfo)
                data = {"caption": full_caption} if full_caption else {}
                _post_telegram(
                    "sendPhoto",
                    data=data,
                    files={"photo": ("image.png", bio, "image/png")},
                )

            if send_as_document:
                bio = _encode_png_with_metadata(pil_img, prompt, extra_pnginfo)
                data = {"caption": full_caption} if full_caption else {}
                _post_telegram(
                    "sendDocument",
                    data=data,
                    files={"document": ("image.png", bio, "image/png")},
                )

        return ()


# ======================================================================
# НОДА: ОТПРАВКА ВИДЕО (принимает VIDEO после Create Video)
# ======================================================================

class TelegramSendVideo:
    """
    Нода для отправки видео в Telegram. Принимает VIDEO (выход Create Video
    и других видео-нод) и caption.
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "video": ("VIDEO",),
                "caption": ("STRING", {"default": "", "multiline": True}),
                "send_as_video": ("BOOLEAN", {"default": True}),
                "send_as_document": ("BOOLEAN", {"default": False}),
            }
        }

    RETURN_TYPES = ()
    RETURN_NAMES = ()
    FUNCTION = "send_video"
    OUTPUT_NODE = True
    CATEGORY = "utils/telegram"

    def send_video(self, video, caption="", send_as_video=True, send_as_document=False):
        if not send_as_video and not send_as_document:
            return ()

        video_path, is_temp = _resolve_video_to_path(video)

        try:
            if send_as_video:
                data = {"supports_streaming": True}
                if caption:
                    data["caption"] = caption
                with open(video_path, "rb") as f:
                    _post_telegram(
                        "sendVideo",
                        data=data,
                        files={"video": (os.path.basename(video_path), f, "video/mp4")},
                        timeout=600,
                    )

            if send_as_document:
                data = {"caption": caption} if caption else {}
                with open(video_path, "rb") as f:
                    _post_telegram(
                        "sendDocument",
                        data=data,
                        files={"document": (os.path.basename(video_path), f, "video/mp4")},
                        timeout=600,
                    )
        finally:
            if is_temp:
                try:
                    os.remove(video_path)
                except Exception:
                    pass

        return ()


# ======================================================================
# НОДА: АЛЬБОМ ИЗОБРАЖЕНИЙ (несколько IMAGE -> один Telegram-альбом)
# ======================================================================

class TelegramSendImageAlbum:
    """
    Отправляет несколько изображений одним альбомом (sendMediaGroup).

    На вход — до 10 опциональных IMAGE-входов (image_1..image_10), каждый из
    которых может быть и батчем. Все подключённые кадры собираются по порядку и
    отправляются альбомами по 10 штук (ограничение Telegram).
    """

    @classmethod
    def INPUT_TYPES(cls):
        optional = {f"image_{i}": ("IMAGE",) for i in range(1, 11)}
        return {
            "required": {
                "caption": ("STRING", {"default": "", "multiline": True}),
                "send_as_photo": ("BOOLEAN", {"default": True}),
                "send_as_document": ("BOOLEAN", {"default": False}),
            },
            "optional": optional,
            "hidden": {
                "prompt": "PROMPT",
                "extra_pnginfo": "EXTRA_PNGINFO",
            },
        }

    RETURN_TYPES = ()
    RETURN_NAMES = ()
    FUNCTION = "send_album"
    OUTPUT_NODE = True
    CATEGORY = "utils/telegram"

    def send_album(
        self,
        caption="",
        send_as_photo=True,
        send_as_document=False,
        prompt=None,
        extra_pnginfo=None,
        **kwargs,
    ):
        if not send_as_photo and not send_as_document:
            return ()

        pil_images = []
        for i in range(1, 11):
            img = kwargs.get(f"image_{i}")
            if img is None:
                continue
            pil_images.extend(_tensor_to_pil_list(img, "TelegramSendImageAlbum"))

        if not pil_images:
            return ()

        if send_as_photo:
            items = [
                (
                    f"image_{idx + 1}.png",
                    _encode_png_with_metadata(im, prompt, extra_pnginfo),
                    "image/png",
                )
                for idx, im in enumerate(pil_images)
            ]
            _send_media_group(items, "photo", caption)

        if send_as_document:
            items = [
                (
                    f"image_{idx + 1}.png",
                    _encode_png_with_metadata(im, prompt, extra_pnginfo),
                    "image/png",
                )
                for idx, im in enumerate(pil_images)
            ]
            _send_media_group(items, "document", caption)

        return ()


# ======================================================================
# НОДА: АЛЬБОМ ВИДЕО (несколько VIDEO -> один Telegram-альбом)
# ======================================================================

class TelegramSendVideoAlbum:
    """
    Отправляет несколько видео одним альбомом (sendMediaGroup).

    На вход — до 10 опциональных VIDEO-входов (video_1..video_10).
    Видео собираются по порядку и отправляются альбомами по 10 штук.
    """

    @classmethod
    def INPUT_TYPES(cls):
        optional = {f"video_{i}": ("VIDEO",) for i in range(1, 11)}
        return {
            "required": {
                "caption": ("STRING", {"default": "", "multiline": True}),
                "send_as_video": ("BOOLEAN", {"default": True}),
            },
            "optional": optional,
        }

    RETURN_TYPES = ()
    RETURN_NAMES = ()
    FUNCTION = "send_album"
    OUTPUT_NODE = True
    CATEGORY = "utils/telegram"

    def send_album(self, caption="", send_as_video=True, **kwargs):
        if not send_as_video:
            return ()

        video_paths = []
        tmp_paths = []

        try:
            for i in range(1, 11):
                v = kwargs.get(f"video_{i}")
                if v is None:
                    continue
                path, is_temp = _resolve_video_to_path(v)
                if is_temp:
                    tmp_paths.append(path)
                video_paths.append(path)

            if not video_paths:
                return ()

            opened = []
            try:
                items = []
                for path in video_paths:
                    f = open(path, "rb")
                    opened.append(f)
                    items.append((os.path.basename(path), f, "video/mp4"))
                _send_media_group(items, "video", caption)
            finally:
                for f in opened:
                    try:
                        f.close()
                    except Exception:
                        pass
        finally:
            for path in tmp_paths:
                try:
                    os.remove(path)
                except Exception:
                    pass

        return ()


# ======================================================================
# РЕГИСТРАЦИЯ НОД ДЛЯ COMFYUI
# ======================================================================

WEB_DIRECTORY = "./web"

NODE_CLASS_MAPPINGS = {
    "TelegramSendImage": TelegramSendImage,
    "TelegramSendVideo": TelegramSendVideo,
    "TelegramSendImageAlbum": TelegramSendImageAlbum,
    "TelegramSendVideoAlbum": TelegramSendVideoAlbum,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "TelegramSendImage": "Telegram: Send Image",
    "TelegramSendVideo": "Telegram: Send Video",
    "TelegramSendImageAlbum": "Telegram: Send Image Album",
    "TelegramSendVideoAlbum": "Telegram: Send Video Album",
}

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
