# config.py
"""
Configuration BACCARAT AI 🤖
Définissez toutes les variables d environnement ci-dessous sur votre plateforme
avant de lancer le bot.

Variables obligatoires :
  - ADMIN_ID
  - PREDICTION_CHANNEL_ID
  - API_ID
  - API_HASH
  - BOT_TOKEN

Variables optionnelles :
  - TELEGRAM_SESSION   (laisser vide pour session automatique)
  - PORT               (defaut : 10000)
  - API_POLL_INTERVAL  (defaut : 5)
"""

import os

def parse_channel_id(value: str) -> int:
    try:
        channel_id = int(value)
        if channel_id > 0 and len(str(channel_id)) >= 10:
            channel_id = -channel_id
        return channel_id
    except:
        raise ValueError(f"ID de canal invalide : {value}")

# ============================================================================
# VARIABLES D ENVIRONNEMENT - OBLIGATOIRES
# ============================================================================

ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
PREDICTION_CHANNEL_ID = parse_channel_id(os.getenv("PREDICTION_CHANNEL_ID", "0"))
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
TELEGRAM_SESSION = os.getenv("TELEGRAM_SESSION", "")

# ============================================================================
# PARAMETRES DU BOT
# ============================================================================

# Port du serveur health check — prend le port de la plateforme, sinon 10000
PORT = int(os.getenv("PORT", "10000"))
API_POLL_INTERVAL = int(os.getenv("API_POLL_INTERVAL", "5"))

# Décalage de prédiction (par défaut a=1)
PREDICTION_OFFSET = int(os.getenv("PREDICTION_OFFSET", "1"))

# ============================================================================
# CONSTANTES — NE PAS MODIFIER
# ============================================================================

# Valeurs des cartes pour le calcul du total
CARD_VALUES = {
    "A": 1, "2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7, "8": 8, "9": 9, "10": 0,
    "J": 0, "Q": 0, "K": 0,
    # Valeurs numériques directes
    "1": 1, "0": 0,
}

SUIT_DISPLAY = {
    "♠": "♠️",
    "♥": "❤️",
    "♦": "♦️",
    "♣": "♣️"
}
