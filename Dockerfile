# Utilise une image Python légère
FROM python:3.11-slim

# Définit le répertoire de travail
WORKDIR /app

# Copie les fichiers nécessaires
COPY requirements.txt .
COPY bot.py .

# Installe les dépendances
RUN pip install --no-cache-dir -r requirements.txt

# Définit la commande pour lancer le bot
CMD ["python", "bot.py"]
