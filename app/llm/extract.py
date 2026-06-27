"""Slot extraction + intent classification.

Two cheap, structured LLM calls used by the graph:

- `extract_slots` reads the latest inbound message and pulls ONLY the facts the
  customer actually gave us, returning a dict to merge into the `Brief`. It never
  invents data.
- `classify_intent` is a router helper that catches objections / global intents
  (too expensive, will think, "are you a bot?", wants a human, etc.) so the graph
  can branch without the per-stage node having to reason about it.
"""
from __future__ import annotations

import logging
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field

from app.graph.state import Brief, Relationship
from app.llm.llm import get_chat

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Slot extraction
# --------------------------------------------------------------------------- #
class SlotExtraction(BaseModel):
    """All-optional mirror of `Brief`. The LLM fills only what the message gives.

    Anything it is unsure about MUST stay `None` (no hallucination).
    """

    recipient_name: Optional[str] = Field(
        None, description="Primeiro nome de quem vai RECEBER a musica (o presenteado)."
    )
    relationship: Optional[Relationship] = Field(
        None, description="Relacao do comprador com o presenteado, normalizada ao enum."
    )
    singer_gender: Optional[str] = Field(
        None,
        description=(
            "'m' ou 'f': genero da VOZ que canta = o COMPRADOR (quem presenteia), "
            "NAO o presenteado. Ex: 'meu marido' -> compradora mulher -> 'f'; "
            "'minha esposa' -> comprador homem -> 'm'."
        ),
    )
    story: Optional[str] = Field(
        None, description="A historia / o que torna a pessoa especial, em poucas palavras."
    )
    special_phrases: Optional[list[str]] = Field(
        None, description="Frases, momentos ou lugares marcantes para citar na letra."
    )
    nickname: Optional[str] = Field(None, description="Apelido carinhoso do presenteado.")
    special_date: Optional[str] = Field(
        None, description="Data marcante (aniversario, quando se conheceram, etc.)."
    )
    style_request: Optional[str] = Field(
        None, description="Estilo musical pedido, texto livre (ex: 'tipo Henrique e Juliano')."
    )


# Buyer-gender cues. The KEY appears in the buyer's message about the RECIPIENT;
# the VALUE is the implied gender of the buyer (= the singer voice).
_BUYER_GENDER_CUES: dict[str, str] = {
    # recipient is male -> buyer (singer) is female
    "meu marido": "f",
    "meu esposo": "f",
    "meu noivo": "f",
    "meu namorado": "f",
    "meu homem": "f",
    "meu amado": "f",
    "meu companheiro": "f",
    # recipient is female -> buyer (singer) is male
    "minha esposa": "m",
    "minha mulher": "m",
    "minha noiva": "m",
    "minha namorada": "m",
    "minha amada": "m",
    "minha companheira": "m",
}


def infer_singer_gender(text: str) -> Optional[str]:
    """Deterministic fallback: map relationship cues to the BUYER's gender.

    Returns 'm' / 'f' or None when no unambiguous cue is present. Mother/father/
    child cues are intentionally ignored (they don't reveal the buyer's gender).
    """
    low = (text or "").lower()
    for cue, gender in _BUYER_GENDER_CUES.items():
        if cue in low:
            return gender
    return None


_EXTRACT_SYSTEM = """\
Voce extrai dados de UMA mensagem de WhatsApp de um cliente que quer encomendar \
uma musica personalizada para presentear alguem.

REGRAS:
- Extraia SOMENTE o que a mensagem realmente fornece. Se nao tiver certeza, deixe \
o campo como null. NUNCA invente, deduza demais ou complete com suposicoes.
- recipient_name = primeiro nome de quem VAI RECEBER a musica (o presenteado).
- relationship = a relacao do COMPRADOR com o presenteado, normalizada para um dos \
valores do enum (esposo, esposa, namorado, namorada, mae, pai, filho, filha, amigo, \
amiga, outro).
- singer_gender ('m'/'f') = genero da VOZ que vai CANTAR, ou seja o COMPRADOR que \
esta presenteando, NAO o presenteado. Pistas: 'meu marido'/'meu namorado' => a \
compradora e mulher => 'f'. 'minha esposa'/'minha namorada' => o comprador e homem \
=> 'm'. Se for mae/pai/filho/amigo e nao der pra saber o genero do comprador, deixe \
null.
- special_phrases = lista de frases, momentos ou lugares marcantes citados.
- Use null para tudo que a mensagem nao trouxer explicitamente.

Considere o brief atual e o estagio so como contexto; nao repita o que ja temos a \
menos que a mensagem traga algo novo."""


async def extract_slots(
    history: list,
    inbound_text: str,
    current_brief: Brief,
    stage: str,
) -> dict:
    """Extract Brief updates from the latest inbound message.

    Returns a dict of NON-None fields to merge into the brief (`special_phrases`
    only when non-empty). Never raises on LLM failure — returns {} instead.
    """
    known = current_brief.model_dump(exclude_none=True)
    user_prompt = (
        f"ESTAGIO ATUAL: {stage}\n"
        f"BRIEF JA CONHECIDO: {known}\n\n"
        f"MENSAGEM DO CLIENTE:\n{inbound_text}"
    )
    messages = [
        ("system", _EXTRACT_SYSTEM),
        *list(history or []),
        ("human", user_prompt),
    ]

    try:
        llm = get_chat(temperature=0.0).with_structured_output(SlotExtraction)
        result: SlotExtraction = await llm.ainvoke(messages)
    except Exception:  # pragma: no cover - network/parse guard
        logger.exception("extract_slots failed; returning no updates")
        result = SlotExtraction()

    updates: dict = {}
    for field, value in result.model_dump(exclude_none=True).items():
        if field == "special_phrases":
            if value:  # drop empty lists
                updates[field] = value
        else:
            updates[field] = value

    # Deterministic fallback for the most error-prone slot.
    if not updates.get("singer_gender"):
        inferred = infer_singer_gender(inbound_text)
        if inferred:
            updates["singer_gender"] = inferred

    return updates


# --------------------------------------------------------------------------- #
# Intent classification
# --------------------------------------------------------------------------- #
class Intent(str, Enum):
    NORMAL = "normal"
    OBJECTION_STYLE = "objection_style"
    OBJECTION_LYRICS = "objection_lyrics"
    TOO_EXPENSIVE = "too_expensive"
    WILL_THINK = "will_think"
    PAY_LATER = "pay_later"
    IS_BOT = "is_bot"
    WANTS_HUMAN = "wants_human"
    GREETING = "greeting"
    QUESTION = "question"


class _IntentResult(BaseModel):
    intent: Intent = Field(Intent.NORMAL, description="Intencao dominante da mensagem.")


_INTENT_SYSTEM = """\
Voce classifica a INTENCAO de uma unica mensagem do cliente numa conversa de venda \
de musica personalizada. Escolha UM rotulo:

- normal: segue o fluxo normalmente (responde uma pergunta, conta a historia, etc.).
- objection_style: nao gostou do estilo / ritmo / batida / voz da musica.
- objection_lyrics: nao gostou da letra, achou generica ou pediu pra mudar a letra.
- too_expensive: reclamou do preco / achou caro.
- will_think: vai pensar, decidir depois, falar com alguem antes.
- pay_later: quer pagar depois / amanha / mais tarde.
- is_bot: pergunta se voce e um robo, uma IA, um atendimento automatico.
- wants_human: pede para falar com um atendente / pessoa / humano de verdade.
- greeting: apenas uma saudacao ou abertura ('oi', 'ola', 'tenho interesse').
- question: uma DUVIDA factual e NEUTRA sobre o produto/processo — como funciona, \
o que a pessoa recebe, formato, prazo de entrega, formas de pagamento, se a musica \
e unica/dela pra sempre, se da pra ouvir antes, se da pra pedir mudanca, ou "quanto \
custa" (pergunta de informacao, SEM reclamar).

PRIORIDADE: se a mensagem reclamar do preco (too_expensive), disser que vai pensar/\
decidir depois (will_think), quiser pagar depois/amanha (pay_later), nao gostou do \
estilo/letra (objection_style/objection_lyrics), for so uma saudacao (greeting), \
pedir um humano (wants_human), ou perguntar se voce e robo/IA (is_bot), use ESSES \
rotulos — NAO 'question'. Ex: "quanto custa?" (neutro) => question; "ta caro / achei \
caro" => too_expensive.

Na duvida, responda 'normal'."""


async def classify_intent(inbound_text: str, stage: str) -> Intent:
    """Cheap structured classification for the router. Defaults to NORMAL."""
    messages = [
        ("system", _INTENT_SYSTEM),
        ("human", f"ESTAGIO: {stage}\nMENSAGEM: {inbound_text}"),
    ]
    try:
        llm = get_chat(temperature=0.0).with_structured_output(_IntentResult)
        result: _IntentResult = await llm.ainvoke(messages)
        return result.intent or Intent.NORMAL
    except Exception:  # pragma: no cover - network/parse guard
        logger.exception("classify_intent failed; defaulting to NORMAL")
        return Intent.NORMAL
