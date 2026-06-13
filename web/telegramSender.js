import { app } from "../../scripts/app.js";

// Регистрируем настройки Telegram Sender в ComfyUI Settings.
// Значения автоматически сохраняются фронтендом в user/default/comfy.settings.json,
// откуда их читает Python-бэкенд ноды (_read_settings_credentials).
app.registerExtension({
    name: "TelegramSender.Settings",
    settings: [
        {
            id: "TelegramSender.BotToken",
            name: "Bot token (от @BotFather)",
            category: ["Telegram Sender", "Credentials", "Bot token"],
            type: "text",
            defaultValue: "",
            tooltip: "Токен бота из @BotFather, например 123456789:ABCdef...",
        },
        {
            id: "TelegramSender.ChatId",
            name: "Chat ID / @channel",
            category: ["Telegram Sender", "Credentials", "Chat ID"],
            type: "text",
            defaultValue: "",
            tooltip: "Числовой ID чата или имя канала вида @my_channel",
        },
    ],
});
