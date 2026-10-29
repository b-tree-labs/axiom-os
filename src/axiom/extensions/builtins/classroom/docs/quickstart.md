# Classroom — Quickstart

A working classroom in 60 seconds. Run the steps in order; the last command at each step prints the next one.

## See it work first (skeptic-eval mode)

```
axi classroom demo
axi classroom prep status demo-classical-mechanics-spring
```

The demo seeds a fully-prepared course with 10 docs of physics content, a 5-student roster, an approved system prompt, and assessments. You can publish + serve it without any further setup, or skip to "Make it your own" below.

## Make it your own (instructor)

```
axi classroom prep init --title "NE 101" --instructor you@university.edu
```

Note the classroom_id printed (UUID). Substitute it for `<CID>` below.

```
axi classroom prep corpus    <CID> --upload syllabus.pdf --upload ch1.pdf --preview "key concepts"
axi classroom prep prompt    <CID> --set "You are a tutor for NE 101..." --test "What is criticality?" --approve
axi classroom prep rag       <CID> --mode course_only
axi classroom prep lms       <CID> --canvas-course <id>     # or --fake for now
axi classroom prep dry-run-enhanced <CID>                   # eyeball a few real student turns
axi classroom publish        <CID> --approver you@university.edu
```

If anything looks off:

```
axi classroom doctor <CID>           # role-aware diagnostic
axi classroom prep status <CID>      # auto-heals the course_selected step
```

## Onboard students

For one student at a time:

```
axi classroom invite <CID> --coordinator-url https://your-server.example
```

For a whole cohort:

```
axi classroom invite <CID> --coordinator-url https://your-server.example --count 12
```

Each invite is single-use — copy one line per student into your roster sheet or LMS announcement. Send them the literal `axi classroom join eyJ...` line.

Now serve the classroom:

```
axi classroom serve <CID> --port 8787
```

## Student side

```
axi classroom join eyJ...                  # literally what your instructor sent
axi classroom ask <CID> "What is criticality?"
axi classroom me  <CID>                    # your latest learning brief
axi classroom me  <CID> --memory           # what's logged about you
axi classroom me  <CID> --forget <id>      # retract a specific question
axi classroom doctor <CID>                 # diagnose if something's off
axi classroom leave <CID>                  # remove your local cache + membership
```

`--memory` shows the coordinator's record of your activity: question count, topics, modes used, recent questions with their interaction ids. `--forget <id>` retracts a specific interaction so it stops surfacing in your brief or the instructor's signals; the coordinator preserves an audit count without the content.

## Instructor day-to-day

```
axi classroom status                       # one-line dashboard across all your classes
axi classroom brief    <CID> --instructor you@university.edu  # CHALKE daily summary
axi classroom briefs   list <CID>          # which students need a brief reviewed
axi classroom briefs   review <CID> <student_id> --approve
axi classroom threads  <CID>               # Q&A from students
axi classroom reply    <CID> <thread_id> "your answer"
axi classroom quiz     broadcast <CID> --bank-preset ne101_core --questions 5 --topic "Week 1"
axi classroom quiz     results   <CID> <quiz_id>
axi classroom evals    <CID> --bank-preset ne101_core --baseline   # measure retrieval lift
```

## End-of-semester

```
axi classroom export <CID> --out semester-final.tar.gz   # full-fidelity bundle for archival
axi classroom archive <CID> --archiver you@university.edu --reason "Spring 2026 concluded"
axi classroom wrap   harvest <CID> --out research.axiompack   # pseudonymized, for sharing
```

`export` is a verbatim instructor keepsake (PII intact). `wrap harvest` produces a research-grade bundle with student principals pseudonymized — use it when sharing with collaborators.

## When things break

`axi classroom doctor <CID>` is the first thing to try. It runs ~6 invariants per role and prints the exact next command for any failure. Exit codes:

- `0` — all green
- `1` — warnings (non-fatal; e.g., coordinator not currently running)
- `2` — failures (something downstream will break)

`--json` is monitor-friendly.

If `doctor` says everything's fine but you're still stuck, capture the output of:

```
axi classroom doctor <CID> --json
axi classroom prep status <CID>
```

…and open an issue at https://github.com/b-tree-labs/axiom-os/issues with both attached.
