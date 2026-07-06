runserver:
	uvicorn main:app --reload

tunnel:
	piperswe-cloudflared.cloudflared --url http://localhost:8000