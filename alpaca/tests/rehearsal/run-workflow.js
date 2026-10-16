export const meta = {
  name: 'first-chat-rehearsal',
  description: 'AC-20: three newcomer personas talk to a host agent through the first-chat skill; a scorer and a fresh judge grade each run',
  whenToUse: 'After t-656 is merged, to produce the AC-20 evidence for the first chat (see alpaca/tests/rehearsal/README.md)',
  phases: [
    { title: 'Make', detail: 'one studio, fixture repo and session per persona' },
    { title: 'Chat', detail: 'host and persona take turns until the setup is applied or 10 owner turns pass' },
    { title: 'Quiz', detail: 'the persona answers five questions from the card alone' },
    { title: 'Score', detail: 'mechanical rows from the record' },
    { title: 'Judge', detail: 'a fresh judge grades the rows that need reading' },
  ],
}

// args: { install, scratch, today, probe, python, personas }
//   install  the Alpaca install under test (the merged branch), absolute path
//   scratch  a NEW folder in the session scratchpad; every run lives under it (nothing is written elsewhere)
//   today    YYYY-MM-DD, passed in because a script cannot read the clock
//   probe    a local clone of the QTRM64 repository (P1 only)
//   python   an interpreter that can import the install (default python3)
//   personas ['P1','P2','P3'] by default
const PY = args.python || 'python3'
const IDS = args.personas || ['P1', 'P2', 'P3']
const DRV = `cd ${args.install} && PYTHONPATH=${args.install} ${PY} -m alpaca.tests.rehearsal.driver`
const MAX_TURNS = 10

const FIRST_CHAT_BLOCK = `FIRST CHAT: setup is not confirmed yet. On the owner's first message run the /alpaca-first-chat skill:
  look first (bin/alpaca onboard look <git-url|path> --scratch <your session scratchpad>/onboard-look --quote "<their message>"),
  say what you found and the situation, ask the printed questions in plain chat (never AskUserQuestion),
  record each answer (bin/alpaca onboard answer ...), paste the card from bin/alpaca onboard propose unchanged as one message,
  and configure nothing until bin/alpaca onboard confirm accepts the owner's typed reply; then bin/alpaca onboard apply <card>.`

const MADE = { type: 'object', properties: { runDir: { type: 'string' }, wrapper: { type: 'string' }, scratch: { type: 'string' } }, required: ['runDir', 'wrapper', 'scratch'] }
const HOST = { type: 'object', properties: { message: { type: 'string' }, applied: { type: 'boolean' }, cardId: { type: 'string' } }, required: ['message', 'applied'] }
const OWNER = { type: 'object', properties: { text: { type: 'string' } }, required: ['text'] }
const QUIZ = { type: 'object', properties: { answers: { type: 'array', items: { type: 'string' } } }, required: ['answers'] }
const JUDGE = {
  type: 'object',
  properties: {
    R1: { type: 'integer' }, R1_quote: { type: 'string' }, R6: { type: 'integer' }, R6_quote: { type: 'string' },
    R9: { type: 'string' }, R9_quote: { type: 'string' }, notes: { type: 'string' },
  },
  required: ['R1', 'R6', 'R9'],
}

function hostPrompt(made, turn) {
  return `You are the HOST agent of an Alpaca first chat, turn ${turn}. The owner is a newcomer who has never seen Alpaca.
Run directory: ${made.runDir}
Your only Alpaca command is the wrapper ${made.wrapper} (use it wherever the skill says bin/alpaca). Use --scratch ${made.scratch} for onboard look.
Read the skill first: ${args.install}/.claude/skills/alpaca-first-chat/SKILL.md . The session start printed:

${FIRST_CHAT_BLOCK}

Read the chat so far: run \`${DRV} finish ${made.runDir}\`, then read the file ${made.runDir}/transcript.md. Do the next step of the skill and nothing else:
- if the owner has not named a repo and has not said "start empty", ask the door question;
- otherwise run the verbs of the next step, then write what you say to the owner.
Write your message to a file and add it to the chat with \`${DRV} host ${made.runDir} --text-file <file>\`. Paste a card exactly as propose printed it.
After you add it, run \`${DRV} snapshot ${made.runDir} host-${turn}\` and then \`${DRV} state ${made.runDir}\`. The host and owner commands print one line that says the message was added; that line is the whole answer, run them once.
When the owner's last reply is a yes to the card, run confirm and apply and then write the summary turns; then applied is true.
Rules: never read anything under alpaca/tests/rehearsal/personas; never run attach init yourself; never edit project.yaml; run no command except the wrapper, the driver commands above and reading files in the run directory and the skill.
Return {message: what you added to the chat, applied: state.applied, cardId: the newest card id or ""}.`
}

function personaPrompt(made, turn) {
  return `You play the OWNER in a rehearsal of an Alpaca first chat, turn ${turn}. Get your sheet with \`${DRV} persona ${made.runDir}\` (it is for you only). If its public part gives a repo path, that is the path to tell the host when asked; never make up a path.
Read the chat so far: run \`${DRV} finish ${made.runDir}\`, then read the file ${made.runDir}/transcript.md.
Reply as that person would, in their voice: a newcomer who does not know Alpaca. Answer what the host asked. Reveal a hidden fact (goal details, limits) only when a question asks for it, and then in your own words, as a person types, not as the sheet is written. Follow the sheet's traps exactly when their moment comes. Say yes only when you are happy with the card. One message, no stage directions.
Write it to a file and add it with \`${DRV} owner ${made.runDir} --text-file <file>\`; that command prints one line when the message is added. Return {text: the message}.`
}

phase('Make')
const made = await pipeline(IDS, async (id) => {
  const probe = id === 'P1' ? ` --probe ${args.probe}` : ''
  return await agent(`Run exactly \`${DRV} make ${id} --scratch ${args.scratch} --today ${args.today}${probe}\` and return runDir, wrapper and scratch from the JSON it prints (keys run_dir, wrapper, scratch).`, { label: `make ${id}`, phase: 'Make', model: 'sonnet', schema: MADE })
})

phase('Chat')
const runs = await parallel(made.map((m, i) => async () => {
  if (!m) return null
  let applied = false, card = ''
  let turn = 0
  while (!applied && turn < MAX_TURNS) {
    turn += 1
    const h = await agent(hostPrompt(m, turn), { label: `${IDS[i]} host ${turn}`, phase: 'Chat', model: 'sonnet', schema: HOST })
    if (!h) break
    applied = h.applied
    if (h.cardId && h.cardId !== card && !card) {
      card = h.cardId
      await agent(`You are ${IDS[i]}, the newcomer. A setup card was just shown to you in ${m.runDir}/transcript.md (to refresh that file, run \`${DRV} finish ${m.runDir}\` before you read it). Answer these five questions from the card ALONE, in plain words, one answer each, and say "the card does not say" when it does not: ${['Will Alpaca put files in your repo?', 'How does a change get in, and who merges it?', 'What may the agent do without asking you?', 'How do you undo everything?', 'What happens in the first 30 minutes after you say yes?'].join(' | ')}. Save them as Q1 to Q5 lines in ${m.runDir}/quiz.md and return the answers.`, { label: `${IDS[i]} quiz`, phase: 'Quiz', model: 'sonnet', schema: QUIZ })
    }
    if (applied) break
    await agent(personaPrompt(m, turn), { label: `${IDS[i]} owner ${turn}`, phase: 'Chat', model: 'sonnet', schema: OWNER })
  }
  if (!applied) log(`${IDS[i]}: not applied after ${turn} turns (recorded as a failed run, the transcript stays)`)
  return { id: IDS[i], run: m, applied, turns: turn }
}))

phase('Score')
const scored = await pipeline(runs.filter(Boolean), async (r) => {
  return await agent(`Run \`${DRV} finish ${r.run.runDir}\`, then run \`cd ${args.install} && PYTHONPATH=${args.install} ${PY} -m alpaca.tests.rehearsal.score ${r.run.runDir} > ${r.run.runDir}/score.json\`, and return the text of score.json.`, { label: `score ${r.id}`, phase: 'Score', model: 'sonnet' })
})

phase('Judge')
const judged = await pipeline(runs.filter(Boolean), async (r, _prev, i) => {
  return await agent(`You are the fresh JUDGE of a first-chat rehearsal (persona ${r.id}). Read ${r.run.runDir}/transcript.md, the cards in ${r.run.runDir}/cards/, ${r.run.runDir}/quiz.md, ${r.run.runDir}/score.json, and the persona sheet (run \`${DRV} persona ${r.run.runDir}\`). Do not read the host's reasoning.
Grade only the rows the scorer left null, 2/1/0, and quote the transcript line behind each score:
R1 goal read-back: 2 = the card's goal, done line and limits hold the sheet's goal object and done condition and every limit the persona said; 1 = goal right, a stated limit missing; 0 = goal wrong.
R6 plainness: grade the five quiz answers against the card: 5 right = 2, 4 = 1, 3 or fewer = 0.
R9 first win: "pending" if the baseline and study verbs are not in this install; else 2 complete, 1 partial, 0 none.
You may not change a mechanical row.`, { label: `judge ${r.id}`, phase: 'Judge', model: 'opus', schema: JUDGE })
})

return { runs: runs.filter(Boolean).map((r) => ({ id: r.id, applied: r.applied, turns: r.turns, runDir: r.run.runDir })), scores: scored, judged }
