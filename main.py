"""
نقطة تشغيل البوت. لا يوجد هنا أي منطق سوى بدء التشغيل.
"""
from bot.core.bot import DiscordBot
from bot.config.settings import settings


def main():
    bot = DiscordBot()
    bot.run(settings.discord_token)


if __name__ == "__main__":
    main()