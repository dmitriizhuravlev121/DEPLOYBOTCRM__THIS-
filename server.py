import os
import asyncio
from aiohttp import web
from testquikbotcrm import main as bot_main

async def health_check(request):
    return web.Response(text="Bot is running!")

async def start_server():
    app = web.Application()
    app.add_routes([web.get('/', health_check)])
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', int(os.getenv('PORT', 8080)))
    await site.start()

async def run_app():
    # Запускаем бот и сервер параллельно
    bot_task = asyncio.create_task(bot_main())
    server_task = asyncio.create_task(start_server())
    await asyncio.gather(bot_task, server_task)

if __name__ == '__main__':
    asyncio.run(run_app())
