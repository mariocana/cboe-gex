FROM python:3.13-slim
RUN pip install --no-cache-dir tzdata
WORKDIR /app
COPY scarica_gex.py struttura.py storico.py app.py ./
ENV PYTHONUNBUFFERED=1
CMD ["python", "app.py"]
