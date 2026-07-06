import shutil
from fastapi import FastAPI, Request, Response, status, UploadFile, File, Form
from fastapi.responses import JSONResponse, RedirectResponse
from contextlib import asynccontextmanager
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from telegram import Update


from tg_bot.init_bot import init_bot





def init_server():
    app = FastAPI(lifespan=lifespan)
    app.mount("/static", StaticFiles(directory="static"), name="static")
    templates = Jinja2Templates(directory="templates")

    @app.get("/")
    async def read_root(request: Request):
        return JSONResponse({"message": "OK"})

    @app.post("/telegram")
    async def get_update(request: Request):
        payload = await request.json()
        update = Update.de_json(payload, request.app.state.bot_app.bot)
        await request.app.state.bot_app.update_queue.put(update)
        return Response(status_code=status.HTTP_200_OK)

    @app.get("/timur")
    async def timur(request: Request):
        await request.app.state.bot_app.bot.send_message(
            chat_id=1668408264, text="кто то зашёл на страницу"
        )
        return JSONResponse({"message": "OK"})
    
    return app


async def lifespan(app: FastAPI):
    # запуск приложения

    bot_app = init_bot()
    app.state.bot_app = bot_app
    await bot_app.initialize()
    await bot_app.start()
    await bot_app.bot.set_webhook(WEBHOOK_URL + "/telegram")
    yield