import re
import asyncio
import logging
import sys
import traceback
from typing import List, Optional, Dict
from datetime import datetime, timezone, timedelta
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors import ChatWriteForbiddenError, UserBannedInChannelError
from aiohttp import web

from config import (
    API_ID, API_HASH, BOT_TOKEN, ADMIN_ID,
    PREDICTION_CHANNEL_ID, PORT, API_POLL_INTERVAL,
    PREDICTION_OFFSET, CARD_VALUES
)
from utils import get_latest_results

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

if not API_ID or API_ID == 0:
    logger.error("API_ID manquant")
    exit(1)
if not API_HASH:
    logger.error("API_HASH manquant")
    exit(1)
if not BOT_TOKEN:
    logger.error("BOT_TOKEN manquant")
    exit(1)

# ============================================================================
# VARIABLES GLOBALES
# ============================================================================

client = None
prediction_channel_ok = False
current_game_number = 0
last_prediction_time: Optional[datetime] = None

# Prédictions en attente de vérification {predicted_game_number: {...}}
pending_predictions: Dict[int, dict] = {}

# Historique des résultats des jeux terminés {game_number: result_dict}
game_results_history: Dict[int, dict] = {}

# Jeux pour lesquels une prédiction a déjà été envoyée
predicted_games: set = set()

# Jeux du joueur (premier groupe) déjà traités pour prédiction
player_games_processed: set = set()

# Dernier numéro de jeu traité pour prédiction
last_processed_game_for_prediction: int = 0

# Historique des prédictions
prediction_history: List[Dict] = []
MAX_HISTORY_SIZE = 100

# ============================================================================
# UTILITAIRES - Calcul des totaux
# ============================================================================

def get_card_value(rank: str) -> int:
    """Retourne la valeur numérique d'une carte pour le calcul du total."""
    rank = str(rank).strip().upper()
    return CARD_VALUES.get(rank, 0)

def calculate_player_total(player_cards: list) -> int:
    """
    Calcule le total du premier groupe (cartes du joueur).
    En Baccarat, on prend le dernier chiffre de la somme (modulo 10).
    """
    total = 0
    for card in player_cards:
        rank = card.get('R', '')
        total += get_card_value(rank)
    return total % 10

def is_even(n: int) -> bool:
    """Vérifie si un nombre est pair."""
    return n % 2 == 0

def predict_next(player_total: int) -> str:
    """
    Détermine la prédiction pour le jeu suivant.
    - Si total pair (0,2,4,6,8) → prédit "pair"
    - Si total impair (1,3,5,7,9) → prédit "impair"
    """
    if is_even(player_total):
        return "pair"
    else:
        return "impair"

def verify_prediction(prediction: str, player_total: int) -> bool:
    """
    Vérifie si la prédiction est correcte.
    - Prédiction "pair" + total pair → ✅
    - Prédiction "impair" + total impair → ✅
    - Sinon → ❌
    """
    pred_is_even = (prediction == "pair")
    total_is_even = is_even(player_total)
    return pred_is_even == total_is_even

# ============================================================================
# UTILITAIRES - Canaux
# ============================================================================

def normalize_channel_id(channel_id) -> Optional[int]:
    if not channel_id:
        return None
    s = str(channel_id)
    if s.startswith('-100'):
        return int(s)
    if s.startswith('-'):
        return int(s)
    return int(f"-100{s}")

async def resolve_channel(entity_id):
    try:
        if not entity_id:
            return None
        normalized = normalize_channel_id(entity_id)
        entity = await client.get_entity(normalized)
        return entity
    except Exception as e:
        logger.error(f"❌ Impossible de résoudre le canal {entity_id}: {e}")
        return None

# ============================================================================
# HISTORIQUE DES PRÉDICTIONS
# ============================================================================

def add_prediction_to_history(game_number: int, prediction: str, triggered_by_game: int, triggered_by_total: int):
    global prediction_history
    prediction_history.insert(0, {
        'predicted_game': game_number,
        'prediction': prediction,
        'triggered_by_game': triggered_by_game,
        'triggered_by_total': triggered_by_total,
        'predicted_at': datetime.now(),
        'status': 'en_cours',
        'verified_at': None,
    })
    if len(prediction_history) > MAX_HISTORY_SIZE:
        prediction_history = prediction_history[:MAX_HISTORY_SIZE]

def update_prediction_history_status(game_number: int, prediction: str, status: str):
    for pred in prediction_history:
        if pred['predicted_game'] == game_number and pred['prediction'] == prediction:
            pred['status'] = status
            pred['verified_at'] = datetime.now()
            break

# ============================================================================
# ENVOI ET MISE À JOUR DES PRÉDICTIONS
# ============================================================================

async def send_prediction(predicted_game: int, prediction: str, triggered_by_game: int, triggered_by_total: int) -> Optional[int]:
    """Envoie une prédiction au canal."""
    global last_prediction_time

    if not PREDICTION_CHANNEL_ID:
        logger.error("❌ PREDICTION_CHANNEL_ID non configuré")
        return None

    prediction_entity = await resolve_channel(PREDICTION_CHANNEL_ID)
    if not prediction_entity:
        logger.error(f"❌ Canal prédiction inaccessible: {PREDICTION_CHANNEL_ID}")
        return None

    msg = (
        f"🌪️ PRÉDICTION #{predicted_game}\n"
        f"😇 Couleur: {prediction}\n"
        f"📊 Statut: 🤔🤔"
    )

    try:
        sent = await client.send_message(prediction_entity, msg)
        last_prediction_time = datetime.now()

        pending_predictions[predicted_game] = {
            'prediction': prediction,
            'triggered_by_game': triggered_by_game,
            'triggered_by_total': triggered_by_total,
            'message_id': sent.id,
            'status': 'en_cours',
            'sent_time': datetime.now(),
        }

        predicted_games.add(predicted_game)
        add_prediction_to_history(predicted_game, prediction, triggered_by_game, triggered_by_total)

        logger.info(f"✅ Prédiction envoyée: #{predicted_game} → {prediction} (déclenché par jeu #{triggered_by_game}, total={triggered_by_total})")
        return sent.id

    except ChatWriteForbiddenError:
        logger.error(f"❌ Pas la permission d'écrire dans le canal {PREDICTION_CHANNEL_ID}")
        return None
    except UserBannedInChannelError:
        logger.error(f"❌ Bot banni du canal {PREDICTION_CHANNEL_ID}")
        return None
    except Exception as e:
        logger.error(f"❌ Erreur envoi prédiction: {e}")
        return None

async def update_prediction_message(predicted_game: int, status: str, trouve: bool):
    """Met à jour le message de prédiction avec le résultat."""

    if predicted_game not in pending_predictions:
        return

    pred = pending_predictions[predicted_game]
    prediction = pred['prediction']
    msg_id = pred['message_id']

    status_emoji = "✅" if trouve else "❌"

    new_msg = (
        f"🎰 PRÉDICTION #{predicted_game}\n"
        f"🎯 Couleur: {prediction}\n"
        f"📊 Statut: {status_emoji}"
    )

    try:
        prediction_entity = await resolve_channel(PREDICTION_CHANNEL_ID)
        if not prediction_entity:
            logger.error("❌ Canal prédiction inaccessible pour mise à jour")
            return

        await client.edit_message(prediction_entity, msg_id, new_msg)
        pred['status'] = status

        status_key = 'gagne' if trouve else 'perdu'
        update_prediction_history_status(predicted_game, prediction, status_key)

        if trouve:
            logger.info(f"✅ Gagné: #{predicted_game} {prediction}")
        else:
            logger.info(f"❌ Perdu: #{predicted_game} {prediction}")

        del pending_predictions[predicted_game]

    except Exception as e:
        logger.error(f"❌ Erreur update message: {e}")

# ============================================================================
# LOGIQUE DE PRÉDICTION ET VÉRIFICATION
# ============================================================================

async def process_game_for_prediction(game_number: int, player_cards: list, is_finished: bool):
    """
    Traite un jeu pour la prédiction.
    
    IMPORTANT: Le bot attend que le jeu du joueur (premier groupe de parenthèses)
    soit FINALISÉ avant de lancer une prédiction.
    
    - Vérifie que le joueur a au moins 2 cartes
    - Attend que le jeu soit finalisé (is_finished=True)
    - Calcule le total et prédit le jeu suivant
    """
    global last_processed_game_for_prediction

    # Vérifier que le joueur a au moins 2 cartes
    if len(player_cards) < 2:
        return

    # ATTENDRE que le jeu du joueur soit finalisé avant de prédire
    if not is_finished:
        logger.debug(f"⏳ Jeu #{game_number}: joueur a ses cartes mais pas encore finalisé → attente prédiction")
        return

    # Éviter de traiter le même jeu plusieurs fois
    if game_number in player_games_processed:
        return

    # Marquer ce jeu du joueur comme traité
    player_games_processed.add(game_number)
    if len(player_games_processed) > 500:
        oldest = min(player_games_processed)
        player_games_processed.discard(oldest)

    # Calculer le total du joueur
    player_total = calculate_player_total(player_cards)

    # Déterminer la prédiction
    prediction = predict_next(player_total)

    # Jeu à prédire = N + a (défaut a=1)
    predicted_game = game_number + PREDICTION_OFFSET

    # Vérifier qu'on n'a pas déjà prédit ce jeu
    if predicted_game in predicted_games:
        logger.info(f"⏸ Jeu #{predicted_game} déjà prédit, ignoré")
        last_processed_game_for_prediction = game_number
        return

    # Vérifier qu'il n'y a pas de prédiction en cours pour le même jeu
    if predicted_game in pending_predictions:
        logger.info(f"⏸ Prédiction en cours pour #{predicted_game}, ignoré")
        last_processed_game_for_prediction = game_number
        return

    # Envoyer la prédiction
    sent = await send_prediction(predicted_game, prediction, game_number, player_total)
    if sent is not None:
        last_processed_game_for_prediction = game_number
        logger.info(f"🔮 Jeu #{game_number} [FINALISÉ]: total joueur={player_total} → prédit #{predicted_game} = {prediction}")

async def verify_pending_predictions(game_number: int, player_cards: list, is_finished: bool):
    """
    Vérifie les prédictions en attente.
    ATTENTION: La vérification attend TOUJOURS que le message soit finalisé (is_finished=True).
    """
    if not is_finished:
        # Ne pas vérifier tant que le jeu n'est pas finalisé
        return

    if len(player_cards) < 2:
        return

    # Calculer le total du joueur pour ce jeu finalisé
    player_total = calculate_player_total(player_cards)

    # Vérifier si ce jeu fait l'objet d'une prédiction en attente
    if game_number in pending_predictions:
        pred = pending_predictions[game_number]
        prediction = pred['prediction']

        # Vérifier la prédiction
        is_correct = verify_prediction(prediction, player_total)

        if is_correct:
            await update_prediction_message(game_number, '✅', True)
        else:
            await update_prediction_message(game_number, '❌', False)

        logger.info(f"🔍 Vérification #{game_number} [FINALISÉ]: prédit={prediction}, total={player_total} → {'✅' if is_correct else '❌'}")

# ============================================================================
# BOUCLE DE POLLING API - DYNAMIQUE
# ============================================================================

async def api_polling_loop():
    """
    Interroge l'API 1xBet en continu.
    
    Comportement :
    - Prédiction: attend que le jeu du joueur soit FINALISÉ avant de prédire.
    - Vérification: attend que le jeu prédit soit finalisé (is_finished=True).
    """
    global current_game_number

    loop = asyncio.get_event_loop()
    logger.info(f"🔄 Polling API dynamique démarré (intervalle: {API_POLL_INTERVAL}s)")

    while True:
        try:
            results = await loop.run_in_executor(None, get_latest_results)

            if results:
                for result in results:
                    game_number = result["game_number"]
                    is_finished = result["is_finished"]
                    player_cards = result.get("player_cards", [])

                    current_game_number = game_number

                    # Stocker le résultat si finalisé
                    if is_finished:
                        game_results_history[game_number] = result
                        # Nettoyer l'historique (garder 500 derniers)
                        if len(game_results_history) > 500:
                            oldest = min(game_results_history.keys())
                            del game_results_history[oldest]

                    # ── 1. LANCER LES PRÉDICTIONS (attendre que le jeu du joueur soit finalisé) ──
                    await process_game_for_prediction(game_number, player_cards, is_finished)

                    # ── 2. VÉRIFIER LES PRÉDICTIONS (attendre que le jeu soit finalisé) ──
                    await verify_pending_predictions(game_number, player_cards, is_finished)

        except Exception as e:
            logger.error(f"❌ Erreur polling API: {e}")
            logger.error(traceback.format_exc())

        await asyncio.sleep(API_POLL_INTERVAL)

# ============================================================================
# RESET COMPLET
# ============================================================================

async def perform_full_reset(reason: str):
    global pending_predictions, last_prediction_time
    global game_results_history, predicted_games
    global last_processed_game_for_prediction, prediction_history
    global player_games_processed

    stats = len(pending_predictions)
    pending_predictions.clear()
    game_results_history.clear()
    predicted_games.clear()
    player_games_processed.clear()
    last_prediction_time = None
    last_processed_game_for_prediction = 0
    prediction_history = []

    logger.info(f"🔄 {reason} - {stats} prédictions cleared")

    try:
        prediction_entity = await resolve_channel(PREDICTION_CHANNEL_ID)
        if prediction_entity and client and client.is_connected():
            await client.send_message(
                prediction_entity,
                f"🔄 **RESET SYSTÈME**\n\n{reason}\n\n"
                f"✅ Compteurs remis à zéro\n"
                f"✅ {stats} prédictions cleared\n\n"
                f"🎰 BACCARAT PREMIUM+2 🎲"
            )
    except Exception as e:
        logger.error(f"❌ Notif reset failed: {e}")

# ============================================================================
# COMMANDES ADMIN
# ============================================================================

async def cmd_status(event):
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return

    lines = [
        "📈 **ÉTAT DU BOT**",
        "",
        f"🔮 Prédictions actives: {len(pending_predictions)}",
        f"📜 Historique résultats: {len(game_results_history)} jeux",
        f"📡 Source: API 1xBet (polling {API_POLL_INTERVAL}s)",
        f"📐 Décalage prédiction: +{PREDICTION_OFFSET}",
        f"🎯 Dernier jeu traité: #{last_processed_game_for_prediction if last_processed_game_for_prediction else 'Aucun'}",
        f"🎮 Jeux joueur traités: {len(player_games_processed)}",
        "",
        "**Règle de prédiction:**",
        "• Attend que le jeu du joueur soit FINALISÉ",
        "• Total joueur pair (0,2,4,6,8) → prédit **pair**",
        "• Total joueur impair (1,3,5,7,9) → prédit **impair**",
        "• Vérification attend le jeu finalisé",
    ]

    if pending_predictions:
        lines.append("")
        lines.append("**🔮 PRÉDICTIONS ACTIVES:**")
        for num, pred in sorted(pending_predictions.items()):
            lines.append(f"• Game #{num}: {pred['prediction']} (déclenché par #{pred['triggered_by_game']}, total={pred['triggered_by_total']})")

    await event.respond("\n".join(lines))

async def cmd_history(event):
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return

    if not prediction_history:
        await event.respond("📜 Aucune prédiction dans l'historique.")
        return

    lines = [
        "📜 **HISTORIQUE DES PRÉDICTIONS**",
        "═══════════════════════════════════════",
        ""
    ]

    for i, pred in enumerate(prediction_history[:20], 1):
        pred_game = pred['predicted_game']
        prediction = pred['prediction']
        trig_game = pred['triggered_by_game']
        trig_total = pred['triggered_by_total']
        time_str = pred['predicted_at'].strftime('%H:%M:%S')

        status = pred['status']
        if status == 'en_cours':
            status_str = "⏳ En cours..."
        elif status == 'gagne':
            status_str = "✅ GAGNÉ"
        elif status == 'perdu':
            status_str = "❌ PERDU"
        else:
            status_str = f"❓ {status}"

        lines.append(
            f"{i}. 🕐 `{time_str}` | **Game #{pred_game}** → {prediction}\n"
            f"   📉 Déclenché par: jeu #{trig_game} (total={trig_total})\n"
            f"   📊 Résultat: {status_str}"
        )
        lines.append("")

    if pending_predictions:
        lines.append("**🔮 PRÉDICTIONS EN ATTENTE:**")
        for num, pred in sorted(pending_predictions.items()):
            lines.append(f"• Game #{num} {pred['prediction']}: En attente de vérification")
        lines.append("")

    lines.append("═══════════════════════════════════════")
    await event.respond("\n".join(lines))

async def cmd_channels(event):
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return

    pred_status = "❌"
    pred_name = "Inaccessible"

    try:
        if PREDICTION_CHANNEL_ID:
            pred_entity = await resolve_channel(PREDICTION_CHANNEL_ID)
            if pred_entity:
                pred_status = "✅"
                pred_name = getattr(pred_entity, 'title', 'Sans titre')
    except Exception as e:
        pred_status = f"❌ ({str(e)[:30]})"

    await event.respond(
        f"📡 **CONFIGURATION**\n\n"
        f"**Source des données:** API 1xBet (polling {API_POLL_INTERVAL}s)\n"
        f"**Jeux en cache:** {len(game_results_history)}\n"
        f"**Décalage prédiction:** +{PREDICTION_OFFSET}\n\n"
        f"**Canal Prédiction:**\n"
        f"ID: `{PREDICTION_CHANNEL_ID}`\n"
        f"Status: {pred_status}\n"
        f"Nom: {pred_name}\n\n"
        f"**Paramètres:**\n"
        f"Dernière prédiction envoyée: #{max(predicted_games) if predicted_games else 'Aucune'}\n"
        f"Admin ID: `{ADMIN_ID}`"
    )

async def cmd_test(event):
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return

    await event.respond("🧪 Test de connexion au canal de prédiction...")

    try:
        if not PREDICTION_CHANNEL_ID:
            await event.respond("❌ PREDICTION_CHANNEL_ID non configuré")
            return

        prediction_entity = await resolve_channel(PREDICTION_CHANNEL_ID)
        if not prediction_entity:
            await event.respond(
                f"❌ **Canal inaccessible** `{PREDICTION_CHANNEL_ID}`\n\n"
                f"Vérifiez:\n"
                f"1. L'ID est correct\n"
                f"2. Le bot est administrateur du canal\n"
                f"3. Le bot a les permissions d'envoi"
            )
            return

        test_msg = (
            f"🌪️ PRÉDICTION #TEST\n"
            f"😇 Couleur: pair\n"
            f"📊 Statut: 🤔🤔"
        )
        sent = await client.send_message(prediction_entity, test_msg)
        await asyncio.sleep(2)

        await client.edit_message(
            prediction_entity, sent.id,
            f"🎰 PRÉDICTION #TEST\n"
            f"🎯 Couleur: pair\n"
            f"📊 Statut: ✅"
        )
        await asyncio.sleep(2)
        await client.delete_messages(prediction_entity, [sent.id])

        pred_name_display = getattr(prediction_entity, 'title', str(prediction_entity.id))
        await event.respond(
            f"✅ **TEST RÉUSSI!**\n\n"
            f"Canal: `{pred_name_display}`\n"
            f"Envoi, modification et suppression: OK"
        )

    except ChatWriteForbiddenError:
        await event.respond(
            "❌ **Permission refusée** — Ajoutez le bot comme administrateur."
        )
    except Exception as e:
        await event.respond(f"❌ Échec du test: {e}")

async def cmd_reset(event):
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return

    await event.respond("🔄 Reset en cours...")
    await perform_full_reset("Reset manuel admin")
    await event.respond("✅ Reset effectué! Compteurs remis à zéro.")

async def cmd_offset(event):
    """Change le décalage de prédiction (a)."""
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return

    parts = event.message.message.strip().split()

    if len(parts) == 1:
        await event.respond(
            f"📐 **DÉCALAGE DE PRÉDICTION**\n\n"
            f"Valeur actuelle: **a = {PREDICTION_OFFSET}**\n"
            f"Prédiction = jeu N + {PREDICTION_OFFSET}\n\n"
            f"Usage: `/offset <valeur>` (ex: `/offset 1`, `/offset 2`)"
        )
        return

    try:
        val = int(parts[1])
        if not 1 <= val <= 10:
            await event.respond("❌ Le décalage doit être entre 1 et 10")
            return
        old_val = PREDICTION_OFFSET
        import config
        config.PREDICTION_OFFSET = val
        await event.respond(
            f"✅ Décalage modifié: {old_val} → {val}\n"
            f"Le bot prédit maintenant le jeu N + {val}"
        )
    except ValueError:
        await event.respond("❌ Valeur invalide. Usage: `/offset 1`")

async def cmd_start(event):
    if event.is_group or event.is_channel:
        return

    await event.respond(
        "🎰 **Bienvenue sur le Bot de Prédiction Baccarat!**\n\n"
        "✨ **Bot de Prédiction Baccarat — Fiable & Précis**\n\n"
        "Ce bot analyse les résultats des jeux en temps réel et prédit:\n"
        "• **pair** si le total du joueur est pair (0,2,4,6,8)\n"
        "• **impair** si le total du joueur est impair (1,3,5,7,9)\n\n"
        "🔥 Prédictions lancées après finalisation du jeu joueur\n"
        "✅ Vérification automatique dès le jeu finalisé\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "📖 Tapez /help pour voir toutes les commandes disponibles.\n"
        "━━━━━━━━━━━━━━━━━━━━━"
    )

async def cmd_help(event):
    if event.is_group or event.is_channel:
        return

    await event.respond(
        "📖 **BACCARAT PREMIUM+2 - AIDE**\n\n"
        "**🎮 Système de prédiction:**\n"
        "• Attend que le jeu du joueur soit FINALISÉ\n"
        "• Calcule le total du premier groupe (cartes joueur)\n"
        "• Total pair (0,2,4,6,8) → prédit **pair** pour N+a\n"
        "• Total impair (1,3,5,7,9) → prédit **impair** pour N+a\n"
        "• a = 1 par défaut (modifiable avec /offset)\n\n"
        "**🔍 Vérification:**\n"
        "• Attend que le jeu prédit soit finalisé\n"
        "• Compare le total du joueur avec la prédiction\n"
        "• Met à jour le message avec ✅ ou ❌\n\n"
        "**🔧 Commandes Admin:**\n"
        "`/status` — État complet du bot\n"
        "`/history` — Historique des prédictions\n"
        "`/channels` — Configuration\n"
        "`/test` — Tester le canal\n"
        "`/offset <val>` — Changer le décalage de prédiction\n"
        "`/reset` — Reset complet\n"
        "`/help` — Cette aide"
    )

async def cmd_announce(event):
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return

    parts = event.message.message.split(' ', 1)
    if len(parts) < 2:
        await event.respond("Usage: `/announce Message`")
        return

    text = parts[1].strip()
    if len(text) > 500:
        await event.respond("❌ Trop long (max 500 caractères)")
        return

    try:
        prediction_entity = await resolve_channel(PREDICTION_CHANNEL_ID)
        if not prediction_entity:
            await event.respond("❌ Canal de prédiction non accessible")
            return

        now = datetime.now()
        msg = (
            f"╔══════════════════════════════════════╗\n"
            f"║     📢 ANNONCE OFFICIELLE 📢          ║\n"
            f"╠══════════════════════════════════════╣\n\n"
            f"{text}\n\n"
            f"╠══════════════════════════════════════╣\n"
            f"║  📅 {now.strftime('%d/%m/%Y')}  🕐 {now.strftime('%H:%M')}\n"
            f"╚══════════════════════════════════════╝\n\n"
            f"🎰 BACCARAT PREMIUM+2 🎲"
        )
        sent = await client.send_message(prediction_entity, msg)
        await event.respond(f"✅ Annonce envoyée (ID: {sent.id})")
    except Exception as e:
        await event.respond(f"❌ Erreur: {e}")

# ============================================================================
# CONFIGURATION DES HANDLERS
# ============================================================================

def setup_handlers():
    client.add_event_handler(cmd_status, events.NewMessage(pattern=r'^/status$'))
    client.add_event_handler(cmd_history, events.NewMessage(pattern=r'^/history$'))
    client.add_event_handler(cmd_start, events.NewMessage(pattern=r'^/start$'))
    client.add_event_handler(cmd_help, events.NewMessage(pattern=r'^/help$'))
    client.add_event_handler(cmd_reset, events.NewMessage(pattern=r'^/reset$'))
    client.add_event_handler(cmd_channels, events.NewMessage(pattern=r'^/channels$'))
    client.add_event_handler(cmd_test, events.NewMessage(pattern=r'^/test$'))
    client.add_event_handler(cmd_offset, events.NewMessage(pattern=r'^/offset'))
    client.add_event_handler(cmd_announce, events.NewMessage(pattern=r'^/announce'))

# ============================================================================
# DÉMARRAGE
# ============================================================================

async def start_bot():
    global client, prediction_channel_ok

    client = TelegramClient(StringSession(TELEGRAM_SESSION), API_ID, API_HASH)

    try:
        await client.start(bot_token=BOT_TOKEN)
        setup_handlers()

        if PREDICTION_CHANNEL_ID:
            try:
                pred_entity = await resolve_channel(PREDICTION_CHANNEL_ID)
                if pred_entity:
                    prediction_channel_ok = True
                    logger.info(f"✅ Canal prédiction OK: {getattr(pred_entity, 'title', 'Unknown')}")
                else:
                    logger.error(f"❌ Canal prédiction inaccessible: {PREDICTION_CHANNEL_ID}")
            except Exception as e:
                logger.error(f"❌ Erreur vérification canal: {e}")

        logger.info(f"🤖 Bot démarré | Décalage prédiction: +{PREDICTION_OFFSET}")
        return True

    except Exception as e:
        logger.error(f"❌ Erreur démarrage: {e}")
        return False

async def main():
    try:
        if not await start_bot():
            return

        asyncio.create_task(api_polling_loop())
        logger.info("🔄 Polling API dynamique démarré")

        app = web.Application()
        app.router.add_get('/health', lambda r: web.Response(text="OK"))
        app.router.add_get('/', lambda r: web.Response(text="BACCARAT PREMIUM+2 🎲 Running"))

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '0.0.0.0', PORT)
        await site.start()

        logger.info(f"🌐 Serveur web démarré sur port {PORT}")

        await client.run_until_disconnected()

    except Exception as e:
        logger.error(f"❌ Erreur main: {e}")
    finally:
        if client and client.is_connected():
            await client.disconnect()
            logger.info("🔌 Déconnecté")

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Arrêté par l'utilisateur")
    except Exception as e:
        logger.error(f"Fatal: {e}")
        sys.exit(1)
