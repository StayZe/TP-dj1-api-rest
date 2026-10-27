FROM python:3.9-slim

WORKDIR /app

# Dépendances d'abord : cette étape reste en cache tant que requirements.txt ne change pas
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Puis le reste du code
COPY . .

EXPOSE 8000

# 0.0.0.0 pour que l'API soit joignable depuis l'extérieur du conteneur
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
