// shield round loop: copy these functions into a Workflow script that builds, checks and fixes.
//
// A workflow script cannot read files, so every model and every policy decision comes from the
// resolver output, passed as the Workflow tool's `args.shield` (the loop keeps no copy of a policy
// rule; only shieldClassDue and shieldStanding use runtime data, and a test compares them with
// alpaca/formation/shield.py):
//   bin/alpaca-python -m alpaca.formation.shield builder-verifier --json
// and the fixer's role id as `args.fixerRole` (builder-verifier: 'builder'; bug-loop: 'fixer').
//
// shieldRounds(t) runs one task through the think stage and the ladder and returns
//   { passed: true, round, reports, briefs, redesigns }                   confirmed; close the task
//   { passed: false, round, reports, briefs, redesigns, action, reason }  blocked; tell the owner
// t carries the prompts, each a function of the round and what is known so far:
//   t.id                      label prefix
//   t.lenses                  [{ key, adversarial?: true, prompt(round) }]  blind check lenses
//   t.mechanical(round)       prompt for the low agent that runs the regression script (and the
//                             task's probe command, when it names one: SKILL.md section 3)
//   t.fix(round, failed, reports, ctx)  prompt for the fixer; reports = investigator and class
//                             reviewer reports so far, this round's included; ctx = { brief,
//                             fixRules, redesign }
//   t.design                  true when the loop checks a design: lenses and fixer run on
//                             ladder.design (ladder.models.design)
//   t.brief                   a think brief this task already has (from its design loop): the
//                             think stage is skipped and the build starts from it
//   t.investigate(round, failed)   prompt for the investigator (brief in SKILL.md)
//   t.judge(round, report)    prompt for the high judge on a disputed finding
//   t.confirm(round)          prompt for the blind confirmer once every lens passes
//   t.classReview(round, reports)  prompt for the class reviewer (SKILL.md section 4)
//   t.followUp                true when the task was made from another task's findings (think full)
//   t.think                   the think stage (SKILL.md section 1b). Give .prompt even with size
//                             'off': a redesign runs the full stage, and without prompts it blocks.
//     .size                   'full' | 'lite' | 'off' (default: full when an adversarial lens or
//                             t.followUp, else the resolver's think.default_size)
//     .hasProbe               true when the task names a probe command
//     .prompt(step, ctx)      prompt for one step: 'trace', 'history', 'probe', 'cause',
//                             'premortem', 'approaches' (ctx.index 0 = patch in place, 1 = remove
//                             the class), 'pick'; ctx = { redesign, reports }

const SHIELD = args.shield.ladder
const SHIELD_FORMATION = args.shield.formations[Object.keys(args.shield.formations)[0]]
const SHIELD_FIXER = SHIELD_FORMATION.roles[args.fixerRole || 'builder']
// A formation may lower the cap, never raise it.
const SHIELD_CAP = Math.min(SHIELD.round_cap, SHIELD_FORMATION.round_cap || SHIELD.round_cap)

// P2 (op-019): prepended to every prompt the loop sends (think stage, lenses, investigator, judge,
// class reviewer, confirmer and fixer), so no agent cites an agent-proposed contract line as the
// owner's authority. The same text is alpaca/taskcontract.py LENS_RULE (a test keeps them equal) and
// `task contract --show` prints it.
const SHIELD_RULES = 'A contract line labelled agent-proposed (or with no origin) is never owner authority; ' +
  'cite the signed brief, an owner-quote line, an owner-proved decision or a batch answer instead.'

const SHIELD_CHECK = {
  type: 'object',
  properties: {
    verdict: { type: 'string', enum: ['pass', 'fail'] },
    blocking: { type: 'array', items: { type: 'string' } },
    file: { type: 'string' },
  },
  required: ['verdict', 'blocking', 'file'],
}
const SHIELD_REPORT = {
  type: 'object',
  properties: {
    cause: { type: 'string', enum: Object.keys(SHIELD.cause_actions) },
    how: { type: 'string' },
    direction: { type: 'string', enum: ['right', 'wrong'] },
    alternative: { type: 'string' },
    disputed: { type: 'array', items: { type: 'string' } },
    file: { type: 'string' },
  },
  required: ['cause', 'how', 'direction', 'file'],
}
const SHIELD_RULING = {
  type: 'object',
  properties: { upheld: { type: 'array', items: { type: 'string' } }, file: { type: 'string' } },
  required: ['upheld', 'file'],
}
const SHIELD_CLASS = {
  type: 'object',
  properties: {
    class: { type: 'string' },        // 'none', or the name of the bug class behind the findings
    removed_by: { type: 'string' },   // the one change that makes the class impossible
    file: { type: 'string' },
  },
  required: ['class', 'file'],
}
const SHIELD_THINK = {
  type: 'object',
  properties: { summary: { type: 'string' }, file: { type: 'string' } },
  required: ['summary', 'file'],
}

// The think stage: the resolver's groups for `size`, one after another, the spawns of a group in
// parallel. Returns the pick's result (the brief), or null when the plan is empty.
async function shieldThink(t, size, ctx) {
  const groups = (Object.hasOwn(SHIELD.think.groups, size) ? SHIELD.think.groups[size] : [])
    .map((g) => g.filter((s) => !s.needs_probe || (t.think && t.think.hasProbe)))
    .filter((g) => g.length)
  const tag = ctx.redesign ? `think${ctx.redesign}` : 'think'
  let brief = null
  for (const group of groups) {
    const results = await parallel(group.map((s) => () =>
      agent(SHIELD_RULES + '\n\n' + t.think.prompt(s.step, { ...ctx, index: s.index }), {
        label: `${t.id} ${tag}-${s.label} r0`, model: s.model, schema: SHIELD_THINK,
      }).then((r) => ({ step: s.step, r }))))
    const pick = results.find((x) => x && x.step === 'pick')
    if (pick) brief = pick.r
  }
  return brief
}

// The size the resolver decided for this task's flags; t.think.size overrides it.
function shieldThinkSize(t) {
  if (t.think && t.think.size) return t.think.size
  const key = `${t.lenses.some((l) => l.adversarial) ? 1 : 0},${t.followUp ? 1 : 0}`
  return SHIELD.think.size_for[key]
}

// Blocking items one check result stands for: none when it passed; else its blocking list, and at
// least one (a check that failed or returned nothing still failed). Used for every result alike.
function shieldStanding(r) {
  if (r && r.verdict === 'pass') return 0
  return Math.max(1, ((r && r.blocking) || []).length)
}

// Same rule as alpaca.formation.shield.class_check_due (compared by a test).
function shieldClassDue(failedRounds, counts) {
  const cc = SHIELD.class_check
  if (!cc) return false
  if (failedRounds >= cc.after_failed_rounds) return true
  const n = counts.length
  return !!cc.non_converging && n >= 2 && counts[n - 1] >= counts[n - 2] && counts[n - 2] > 0
}

async function shieldRounds(t) {
  const M = SHIELD.models[t.design ? 'design' : 'build']
  let fixerTier = SHIELD_FIXER.tier
  let failing = t.lenses
  const reports = []
  const briefs = []
  const canThink = !!(t.think && typeof t.think.prompt === 'function')
  let redesigns = 0
  let failedRounds = 0          // since the last think stage
  let counts = []               // blocking items per failed round, since the last think stage
  let classChecked = false      // the class reviewer already ran in this think cycle
  const out = (o) => ({ reports, briefs, redesigns, ...o })
  const block = (round, reason) => out({ passed: false, round, action: SHIELD.at_cap, reason })
  const thinkNeeded = (size) => Object.hasOwn(SHIELD.think.groups, size) &&
    SHIELD.think.groups[size].length > 0
  const fixModel = () => M.fixer || SHIELD.tiers[fixerTier]

  // Once per task: a build loop that follows a design loop passes the design loop's brief.
  const size = t.brief ? 'off' : shieldThinkSize(t)
  if (!Object.hasOwn(SHIELD.think.groups, size)) return block(0, `unknown think size ${size}`)
  if (!fixModel()) return block(0, `the fixer role's tier ${fixerTier} maps to no model`)
  if (thinkNeeded(size) && !canThink) {
    return block(0, `think stage size ${size} but the task gives no think prompts (t.think.prompt)`)
  }
  let brief = t.brief || await shieldThink(t, size, { redesign: 0, reports })
  if (brief && !t.brief) briefs.push(brief)

  for (let round = 1; round <= SHIELD_CAP; round++) {
    const label = (s) => `${t.id} ${s} r${round}`
    const [mech, ...lensResults] = await parallel([
      () => agent(SHIELD_RULES + '\n\n' + t.mechanical(round), { label: label('regression'), model: M.regression,
        effort: 'low', schema: SHIELD_CHECK }),
      ...failing.map((l) => () => agent(SHIELD_RULES + '\n\n' + l.prompt(round), { label: label(l.key), schema: SHIELD_CHECK,
        model: l.adversarial ? M.adversarial_lens : M.judgment_lens })),
    ])
    const resultOf = Object.fromEntries(failing.map((l, i) => [l.key, lensResults[i]]))
    let failed = failing.filter((l) => shieldStanding(resultOf[l.key]) > 0)
    const mechFailed = shieldStanding(mech) > 0

    // At most one confirmer per round. A reopen is a finding against the whole deliverable: no
    // ruling on lens keys can clear it, and only a fix and a full recheck lead to the next one.
    let reopened = null
    const confirm = async () => {
      const c = await agent(SHIELD_RULES + '\n\n' + t.confirm(round), { label: label('confirm'), model: M.confirmer,
        schema: SHIELD_CHECK })
      if (c && c.verdict === 'pass') return true
      reopened = c || { verdict: 'fail', blocking: [] }
      failed = t.lenses           // every lens checks again after the fix
      return false
    }
    if (!mechFailed && !failed.length && await confirm()) return out({ passed: true, round })
    if (round === SHIELD_CAP) return block(round, `round cap ${SHIELD_CAP} reached without a confirmed pass`)

    // The investigator finishes before the fix starts, and the fix reads its report.
    const rep = await agent(SHIELD_RULES + '\n\n' + t.investigate(round, failed), { label: label('investigate'),
      model: M.investigator, schema: SHIELD_REPORT })
    let action = 'continue'
    let dismissed = new Set()
    if (rep) {
      reports.push(rep)
      action = SHIELD.cause_actions[rep.cause] || 'continue'
      if (action === 'cancel-and-ask-owner') {
        return out({ passed: false, round, action, reason: `${rep.cause}: ${rep.how}` })
      }
      if (action === 'promote-fixer') fixerTier = SHIELD.promote_next[fixerTier] || fixerTier
      if (action === 'judge-dispute' && failed.length && !reopened) {
        const ruling = await agent(SHIELD_RULES + '\n\n' + t.judge(round, rep), { label: label('judge'),
          model: M.judge, schema: SHIELD_RULING })
        if (ruling) {
          dismissed = new Set(failed.filter((l) => !ruling.upheld.includes(l.key)).map((l) => l.key))
          failed = failed.filter((l) => !dismissed.has(l.key))
        }
        // Every finding of this round overruled and no confirmer has run yet: confirm now.
        if (!mechFailed && !failed.length && await confirm()) return out({ passed: true, round })
      }
    }

    // Blocking items that still stand: the regression result, every lens not overruled, a reopen.
    const blocking = shieldStanding(mech)
      + failing.filter((l) => !dismissed.has(l.key)).reduce((n, l) => n + shieldStanding(resultOf[l.key]), 0)
      + (reopened ? shieldStanding(reopened) : 0)
    failedRounds += 1
    counts.push(blocking)
    let redesign = action === 'redesign'
    // The class reviewer runs at most twice per think cycle: once when the rounds stop
    // converging, once more when the failed-round count reaches its limit.
    const atLimit = SHIELD.class_check && failedRounds === SHIELD.class_check.after_failed_rounds
    if (!redesign && shieldClassDue(failedRounds, counts) && (!classChecked || atLimit)) {
      classChecked = true
      const cls = await agent(SHIELD_RULES + '\n\n' + t.classReview(round, reports), { label: label('class'),
        model: M.class, schema: SHIELD_CLASS })
      const found = cls && cls.class && cls.class.toLowerCase() !== 'none'
      if (found) redesign = true
      if (cls) reports.push({ cause: 'class-check', how: cls.class, removed_by: cls.removed_by,
        file: cls.file })
    }
    if (redesign) {
      if (redesigns >= SHIELD.max_redesigns) {
        return block(round, redesigns ? 'the same failure class persists after the last redesign'
          : 'a redesign is due but the policy allows none (escalation.class_check)')
      }
      if (thinkNeeded(SHIELD.think.redesign_size) && !canThink) {
        return block(round, 'a redesign is due but the task gives no think prompts (t.think.prompt)')
      }
      redesigns += 1
      const fresh = await shieldThink(t, SHIELD.think.redesign_size, { redesign: redesigns, reports })
      if (fresh) { brief = fresh; briefs.push(fresh) }
      failedRounds = 0
      counts = []
      classChecked = false
    }
    await agent(SHIELD_RULES + '\n\n' + t.fix(round, failed, reports, { brief, fixRules: SHIELD.fix_rules, redesign }), {
      label: label('fix'), model: fixModel() })
    failing = redesign ? t.lenses : failed
  }
}
