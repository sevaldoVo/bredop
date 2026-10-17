"""Заглушка TG-OSINT API (для теста VILKA_TG_API_URL). Запуск: uvicorn tg_stub:app --port 9099"""
from fastapi import FastAPI
app = FastAPI()

@app.post("/tg/check")
async def check(body: dict):
    phone = str(body.get("phone", ""))
    if phone.endswith("0000"):  # «боевые» номера в тесте
        return {"found": True, "tg_id": "334455667", "username": "ivan_petrov"}
    return {"found": False}
