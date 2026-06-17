"""Songwriter node: Brief -> KiePayload.

The most important LLM call. It turns the collected `Brief` into a fully-formed
KIE/Suno request whose lyrics follow the invariants distilled from
music-pipeline/prompts/*.json:

- pt-BR lyrics WITHOUT accents
- opens with a short verse (NO name), intro under 3 seconds
- the recipient's NAME appears ONLY in the chorus (never at the start), with a
  full verse right after the first chorus
- weave in special_phrases / nickname / special_date
- vocalGender = the SINGER/BUYER's gender (brief.singer_gender), NOT the recipient

We force the schema with structured output, then deterministically override the
fields the graph owns (style, vocalGender, title, call_back_url) and clamp the
weights so a stray model value can never break the KIE contract. We do NOT import
the sanitizer or KIE client — the graph sanitizes and submits.
"""
from __future__ import annotations

import logging
from typing import Optional

from app.config import settings
from app.graph.state import Brief
from app.llm.llm import get_chat
from app.music.schema import KiePayload

logger = logging.getLogger(__name__)

# Weight envelopes (from the prompt templates).
_STYLE_WEIGHT_LO, _STYLE_WEIGHT_HI = 0.72, 0.78
_AUDIO_WEIGHT_LO, _AUDIO_WEIGHT_HI = 0.68, 0.72

# Always-on negative tags regardless of genre.
_BASE_NEGATIVES = ["long instrumental intro", "autotune heavy"]


# --------------------------------------------------------------------------- #
# Few-shot exemplars (compact, copied from music-pipeline/prompts/*.json)
# --------------------------------------------------------------------------- #
_EXEMPLARS: list[dict] = [
    {
        "tags": ["pop", "acustica", "acustico", "indie", "folk"],
        "title": "Para Lucas",
        "vocalGender": "f",
        "style": (
            "brazilian pop romantic acoustic indie folk, warm female voice singing "
            "to husband, BPM 100, acoustic guitar strumming, ukulele, soft claps, "
            "very short intro under 3 seconds then voice immediately, no autotune"
        ),
        "prompt": (
            "[Verse 1 - opens song, bright acoustic strum immediately, no name]\n"
            "A gente se conheceu sem esperar\n"
            "E aqui estamos, anos depois, dancando na cozinha\n\n"
            "[Chorus]\n"
            "Lucas, oito anos e tudo continua\nLucas, voce me faz rir do nada\n"
            "Cada dia ao seu lado e leve e e simples\nLucas, voce e a minha casa\n\n"
            "[Verse 2]\n"
            "Nos dias dificeis voce nao solta a minha mao\n"
            "Na correria do mundo voce e o meu chao"
        ),
    },
    {
        "tags": ["sertanejo", "viola", "sanfona", "mae", "filho"],
        "title": "Para Rosangela",
        "vocalGender": "m",
        "style": (
            "sertanejo romantico brasileiro moderno, voz masculina jovem clara forte "
            "afinada bem na frente do mix, diccao limpa, violao dedilhado intro curta, "
            "viola caipira, sanfona entrando no refrao, intro curta menos de 3 segundos, "
            "BPM 80"
        ),
        "prompt": (
            "[Verse 1 - opens song, violao dedilhado, voice clear and strong, no name]\n"
            "Tem uma mulher que mudou a minha historia\n"
            "Foi quem me ensinou a nunca desistir\n\n"
            "[Chorus - full instrumentation, vocal na frente]\n"
            "Mae amorosa, mae carinhosa\n"
            "Rosangela, minha vida toda\n"
            "Rosangela, minha rainha, meu porto, meu ceu\n\n"
            "[Verse 2]\n"
            "Lembro do cheiro do cafe de manha\n"
            "Da fe que voce nunca deixou faltar"
        ),
    },
    {
        "tags": ["pagode", "samba", "cavaquinho", "roda"],
        "title": "Para Ana",
        "vocalGender": "m",
        "style": (
            "brazilian pagode samba romantic, warm male lead voice with light backing "
            "vocals, BPM 98, cavaquinho, pandeiro, surdo, violao sete cordas, very short "
            "cavaquinho intro under 3 seconds then voice immediately"
        ),
        "prompt": (
            "[Verse 1 - opens song, cavaquinho lick into full samba groove immediately, no name]\n"
            "Na roda de samba eu te encontrei\n"
            "Voce mexeu comigo de uma vez\n\n"
            "[Chorus]\n"
            "Ana, voce e a minha melodia\n"
            "Ana, meu samba de toda alegria\n"
            "Ana, contigo a vida e cantoria\n\n"
            "[Verse 2]\n"
            "Cada batida do pandeiro lembra voce\n"
            "Cada acorde e um pedaco do nosso bem"
        ),
    },
    {
        "tags": ["mpb", "balada", "ballad", "piano"],
        "title": "Para Juliana",
        "vocalGender": "m",
        "style": (
            "brazilian MPB ballad romantic, soft warm male voice classic style, BPM 80, "
            "piano lead, soft strings, very short piano intro under 3 seconds then chorus "
            "immediately"
        ),
        "prompt": (
            "[Verse 1 - opens song, piano arpeggio leading into voice immediately, no name]\n"
            "No meio da tempestade voce chegou\n"
            "Trouxe a calma que faltava no meu coracao\n\n"
            "[Chorus]\n"
            "Juliana, do meu coracao\n"
            "Juliana, minha calma na tempestade\n"
            "Juliana, contigo quero sempre estar\n\n"
            "[Verse 2]\n"
            "Cada amanhecer ao seu lado vira cancao\n"
            "Cada silencio nosso ja e paz"
        ),
    },
    {
        "tags": ["gospel", "religioso", "fe", "deus"],
        "title": "Para Maria",
        "vocalGender": "m",
        "style": (
            "brazilian sertanejo gospel acoustic ballad, intimate emotional male voice, "
            "BPM 75, acoustic guitar fingerpicking, soft piano, very short intro under 3 "
            "seconds then voice immediately, deep religious gratitude"
        ),
        "prompt": (
            "[Verse 1 - opens song, warm acoustic + soft strings immediately, no name]\n"
            "Foi Deus quem cruzou o meu caminho com o seu\n"
            "Uma bencao que eu nem sei se mereci\n\n"
            "[Chorus]\n"
            "Maria, foi Deus quem te trouxe pra mim\n"
            "Maria, minha bencao do amanhecer\n"
            "Maria, contigo eu quero envelhecer\n\n"
            "[Verse 2]\n"
            "Na oracao eu agradeco por voce\n"
            "Cada dia a nossa fe so faz crescer"
        ),
    },
]


def _select_exemplars(style_resolved: str, style_request: Optional[str], k: int = 3) -> list[dict]:
    """Pick up to `k` few-shot exemplars, preferring the requested genre."""
    haystack = f"{style_resolved} {style_request or ''}".lower()
    matched = [ex for ex in _EXEMPLARS if any(tag in haystack for tag in ex["tags"])]
    if not matched:
        # default to one female + one male voice exemplar for broad coverage
        matched = [_EXEMPLARS[0], _EXEMPLARS[1]]
    # top up to k with non-duplicates, keeping genre matches first
    for ex in _EXEMPLARS:
        if len(matched) >= k:
            break
        if ex not in matched:
            matched.append(ex)
    return matched[:k]


def _build_system_prompt(exemplars: list[dict]) -> str:
    shots = "\n\n".join(
        f"# EXEMPLO ({', '.join(ex['tags'][:2])})\n"
        f"title: {ex['title']}\nvocalGender: {ex['vocalGender']}\n"
        f"style: {ex['style']}\nprompt:\n{ex['prompt']}"
        for ex in exemplars
    )
    return f"""\
Voce e um compositor de musicas personalizadas em portugues do Brasil para a Suno \
(via KIE). Voce recebe um brief e escreve a LETRA e o pedido de geracao.

INVARIANTES OBRIGATORIAS (sempre, sem excecao):
1. A letra (campo prompt) e em portugues do Brasil SEM ACENTOS (nada de a, e, ~). \
Escreva 'coracao' e nao 'coracao' com til, 'voce' e nao 'voce' com acento, etc.
2. A musica ja entra CANTANDO (introducao instrumental MENOS DE 3 SEGUNDOS), mas \
ABRE POR UM VERSO curto que NAO contem o nome — o nome NUNCA aparece no comecinho \
da musica. Marque a primeira secao (ex: '[Verse 1 - opens song, ... immediately, no name]').
3. O NOME do presenteado aparece SOMENTE no refrao ([Chorus]) — nunca nos versos, \
nunca na ponte, nunca na abertura. No refrao, repita o nome. O PRIMEIRO refrao (onde \
o nome aparece) deve chegar cedo, de preferencia dentro dos primeiros ~45 segundos, e \
logo depois dele deve vir um VERSO completo.
4. Costure organicamente as special_phrases, o apelido (nickname) e a data \
especial (special_date) na letra quando existirem.
5. Estrutura tipica: [Verse 1] (sem nome) -> [Chorus] (com o nome) -> [Verse 2] (sem \
nome) -> [Pre-Chorus] -> [Chorus] -> [Bridge] -> [Chorus] -> [Outro]. Use marcadores \
em ingles entre colchetes.
6. vocalGender = o genero de quem CANTA (o comprador que presenteia), fornecido no \
brief. NAO e o genero do presenteado.
7. title sempre no formato 'Para <nome do presenteado>'.
8. negativeTags devem excluir generos concorrentes, 'long instrumental intro' e \
'autotune heavy'.
9. styleWeight entre 0.72 e 0.78; audioWeight entre 0.68 e 0.72.

Escreva uma letra emocionante, especifica e irreplicavel — a historia DELES.

{shots}"""


def _clamp(value: float, lo: float, hi: float, default: float) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


def _merge_negative_tags(existing: str) -> str:
    parts = [t.strip() for t in (existing or "").split(",") if t.strip()]
    low = {t.lower() for t in parts}
    for required in _BASE_NEGATIVES:
        if required not in low:
            parts.append(required)
            low.add(required)
    return ", ".join(parts)


def _build_user_prompt(brief: Brief, style_resolved: str) -> str:
    b = brief
    return (
        "BRIEF:\n"
        f"- recipient_name (presenteado): {b.recipient_name}\n"
        f"- relationship: {getattr(b.relationship, 'value', b.relationship)}\n"
        f"- singer_gender (quem CANTA / vocalGender): {b.singer_gender}\n"
        f"- story: {b.story}\n"
        f"- special_phrases: {b.special_phrases}\n"
        f"- nickname: {b.nickname}\n"
        f"- special_date: {b.special_date}\n"
        f"- style_request (texto livre do cliente): {b.style_request}\n\n"
        f"STYLE RESOLVIDO (use como referencia de sonoridade): {style_resolved}\n\n"
        "Gere o KiePayload completo. Capriche na letra seguindo TODAS as invariantes."
    )


async def write_song(
    brief: Brief,
    style_resolved: str,
    *,
    call_back_url: str | None = None,
) -> KiePayload:
    """Turn a `Brief` into a validated `KiePayload`. Retries once on failure."""
    exemplars = _select_exemplars(style_resolved, brief.style_request)
    system_prompt = _build_system_prompt(exemplars)
    user_prompt = _build_user_prompt(brief, style_resolved)
    messages = [("system", system_prompt), ("human", user_prompt)]

    vocal_gender = brief.singer_gender if brief.singer_gender in ("m", "f") else "m"
    recipient = (brief.recipient_name or "voce").strip()

    last_err: Optional[Exception] = None
    for attempt in range(2):
        try:
            llm = get_chat(settings.openai_songwriter_model, temperature=0.8)
            draft: KiePayload = await llm.with_structured_output(KiePayload).ainvoke(messages)

            # Deterministically override graph-owned fields + clamp weights so a
            # stray model value can never break the KIE contract.
            payload = draft.model_copy(
                update={
                    # We always author the full lyrics, so custom mode is an
                    # invariant: it lets `prompt` be the exact lyrics (up to 5000
                    # chars). A stray `customMode:false` from the model caps the
                    # prompt at 500 chars and KIE rejects it with a 422.
                    "custom_mode": True,
                    "title": f"Para {recipient}",
                    "style": style_resolved,
                    "vocal_gender": vocal_gender,
                    "style_weight": _clamp(draft.style_weight, _STYLE_WEIGHT_LO, _STYLE_WEIGHT_HI, 0.75),
                    "audio_weight": _clamp(draft.audio_weight, _AUDIO_WEIGHT_LO, _AUDIO_WEIGHT_HI, 0.70),
                    "negative_tags": _merge_negative_tags(draft.negative_tags),
                    "instrumental": False,
                    "call_back_url": call_back_url,
                }
            )
            # Re-validate the final object against the schema.
            return KiePayload.model_validate(payload.model_dump(by_alias=True))
        except Exception as exc:  # noqa: BLE001 - retry once on any parse/validation error
            last_err = exc
            logger.warning("write_song attempt %d failed: %s", attempt + 1, exc)

    raise RuntimeError(f"write_song failed after retry: {last_err}")
