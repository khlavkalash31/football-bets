# Immagine dell'app Streamlit (vedi docker-compose.yml). Il codice è montato come volume,
# quindi le modifiche ai file si vedono ricaricando la pagina, senza ricostruire.
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
EXPOSE 8501
CMD ["streamlit", "run", "app/app.py", "--server.address=0.0.0.0", "--server.port=8501"]
