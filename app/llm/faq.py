"""Grounded FAQ facts for answering customer doubts/questions.

Single source of truth for the product FACTS Marina may state when a customer
asks a doubt ("how does it work?", "how do I pay?", "how long does it take?").
The router catches `Intent.QUESTION` and feeds `faq_instruction(stage)` to
`reply.compose`, so answers stay accurate and never hallucinate price / format /
delivery / payment.

IMPORTANT: this file — NOT sales/whatsapp-script.md — is the correct source for
payment. The script still says "chave pix"; the live system uses an InfinitePay
**checkout link** (card or pix inside the link). Keep the facts here in sync with
the real flow, with price/regens interpolated from `settings` so they never drift.

Written WITHOUT accents, like the other LLM instruction prompts (extract.py).
"""
from __future__ import annotations

from app.config import settings
from app.graph.state import Stage

# Ground truth Marina is allowed to state. Price/regens come from settings.
FAQ_FACTS = f"""\
FATOS (use SOMENTE estes; nunca invente):
- E uma musica 100% personalizada com a historia da pessoa. E unica: so existe pra voces.
- Preco: R$ {settings.price_reais} (valor unico, sem taxa escondida).
- Como funciona: a gente conversa pra montar a historia -> eu mando uma PREVIA de uns \
45 segundos (em video) DE GRACA pra tu ouvir antes -> se gostar, e so pagar -> ai eu \
mando a musica completa.
- A previa e gratis e vem ANTES de qualquer pagamento; junto vai um pedaco da letra.
- Pagamento: e um LINK de checkout — da pra pagar no cartao ou no pix, tudo dentro do \
mesmo link. So depois que tu aprovar a previa.
- A musica completa chega uns 2 minutinhos depois do pagamento, como um VIDEO 9:16 que \
toca direto aqui no WhatsApp. A letra inteira eu mando se tu pedir.
- A musica fica pronta em uns 2 a 3 minutos.
- Se nao gostar do estilo ou da letra, eu refaco com carinho (ate \
{settings.max_free_regenerations} vez sem custo).
- Tudo e entregue aqui pelo WhatsApp mesmo.
- Serve pra qualquer pessoa e ocasiao (esposo, esposa, mae, pai, filho, amigo... \
aniversario, dia das maes, casamento, ou so porque sim)."""

# "Goal of the current stage" — used to bridge back into the funnel after answering.
_STAGE_NUDGE: dict[str, str] = {
    Stage.WELCOME.value: "volte com leveza a perguntar pra quem e a musica",
    Stage.DISCOVERY_RECIPIENT.value: "volte a perguntar o nome de quem vai receber",
    Stage.DISCOVERY_STORY.value:
        "volte a perguntar o que torna essa pessoa especial / a historia de voces",
    Stage.STYLE.value: "volte a perguntar qual estilo musical combina mais com voces",
    Stage.ANCHOR.value: "pergunte de novo, sem pressao, se pode comecar a gerar",
    Stage.SONGWRITER.value: "lembre com carinho que a musica ja vai comecar a ser feita",
    Stage.GENERATE.value: "lembre com carinho que a musica ja esta sendo feita",
    Stage.GENERATION_WAIT.value: "lembre que a musica ja esta sendo feita, e so uns minutinhos",
    Stage.PREVIEW.value: "convide pra ouvir a previa com calma e pergunte qual parte mexeu mais",
    Stage.CHOICE.value: "projete a reacao de quem vai receber e pergunte o que mais tocou",
    Stage.PIX.value: "lembre que e so finalizar o pagamento no link pra tu receber a completa",
    Stage.PIX_WAIT.value:
        "lembre que e so finalizar o pagamento no link que ai tu recebe a completa",
    Stage.VERIFY.value: "lembre que assim que o pagamento cair tu recebe a musica completa",
    Stage.DELIVER.value: "retome com leveza",
    Stage.FOLLOWUP.value: "retome com leveza",
    Stage.DONE.value: "retome com leveza",
}
_DEFAULT_NUDGE = "depois volte com leveza pro ponto onde voces estavam"

# Stages where price should be deferred (script rule 5: "preco so depois da historia").
_DISCOVERY_STAGES = {
    Stage.WELCOME.value,
    Stage.DISCOVERY_RECIPIENT.value,
    Stage.DISCOVERY_STORY.value,
    Stage.STYLE.value,
}


def faq_instruction(stage: str, *, deflect_bot: bool = False) -> str:
    """Build the per-turn instruction for answering a doubt at `stage`.

    Pairs with PERSONA_SYSTEM inside reply.compose. Grounds the answer in
    FAQ_FACTS, forbids invention, and bridges back to the current stage's goal
    without advancing the funnel. `deflect_bot` adds the in-character "are you a
    bot?" deflection.
    """
    nudge = _STAGE_NUDGE.get(stage, _DEFAULT_NUDGE)
    parts = [
        FAQ_FACTS,
        "Responda a ULTIMA pergunta/duvida do cliente de forma CURTA e calorosa, "
        "usando SOMENTE os fatos acima.",
        "NUNCA invente preco, formato, prazo ou forma de pagamento. Se a duvida nao "
        "estiver nos fatos, seja honesta: diga que confirma rapidinho — nao chute.",
        "Pagamento e sempre o LINK de checkout (cartao ou pix pelo link). Nunca diga "
        "'chave pix' nem 'copia e cola'.",
        f"Depois de responder, faca uma ponte gentil de volta: {nudge}. Nao avance "
        "etapas nem repita perguntas que ela ja respondeu.",
    ]
    if stage in _DISCOVERY_STAGES:
        parts.append(
            "Se a duvida for sobre PRECO e voces ainda estao no comeco (descoberta), "
            "nao jogue o numero seco: diga com leveza que e super acessivel e que tu ja "
            "explica certinho como funciona, e volte pra historia. Se a pessoa INSISTIR "
            f"no valor, ai pode dizer R$ {settings.price_reais} com naturalidade."
        )
    if deflect_bot:
        parts.append(
            "A pessoa perguntou se voce e um robo/IA. Voce e a Marina e voce FAZ as "
            "musicas com carinho — responda leve e humana, SEM confirmar que e IA e SEM "
            "usar as palavras 'robo', 'IA' ou 'inteligencia artificial', e siga a "
            f"conversa reconduzindo: {nudge}."
        )
    return "\n\n".join(parts)
