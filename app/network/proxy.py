from aiogram.client.session.aiohttp import AiohttpSession
from app.config import PROXY_URL

session = AiohttpSession(
    proxy=PROXY_URL
)