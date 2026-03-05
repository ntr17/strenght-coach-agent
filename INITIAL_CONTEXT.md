# INITIAL CONTEXT

> **Note:** This is the history of how this project started. It's context for understanding what the user wants, not a rigid specification. Things evolve.

## How This Started

The user has been working with an AI coach on a strength training program. Over many conversations, they discussed:
- Training programming (currently a 30-week strength program, but this varies)
- Nutrition (insulin resistance, needs to eat more)
- Recovery (sleep, walking, stress)
- Life situation (demanding job, frequent travel)
- Coaching style (direct, honest, no pandering)

The idea for this project came from wanting to automate the coaching feedback:
> "Can you help me create an AI agent so that you have access to the spreadsheet, we give some instructions and you give tips seeing my numbers?"

## What The User Wants

### Core Idea
A system that:
1. Looks at their training data daily
2. Analyzes progress intelligently
3. Sends them a coaching email
4. Can answer questions and make suggestions
5. Adapts over time

### Key Quotes (what shaped the requirements)

On input flexibility:
> "Yo quiero poder escribir prompts enteros con mil preguntas y demás, pero también que pueda handlear días con poca o nula info."

On not wanting to repeat data:
> "NO QUIERO UN LOG DONDE TENGA QUE PONER MANUALMENTE LOS PESOS. Quiero mis semanas de entrenamiento donde pongo Yes a lo que hice y sensaciones."

On long-term thinking:
> "100 ahora es una utopía pero por ejemplo en 3 años estoy estancado en 130 y me sigue diciendo que muy bien por haber superado los 100. Quiero que tenga esa visión de mejora constante."

On honest coaching:
> "You are not exhaustive or honest. Were I to tell you running is better, you would have supported me."

On the system adapting:
> "The programs don't have to be 30 weeks always. The context changes of who I am, my goals etc."

## Ideas Discussed (not final decisions)

### Multi-Agent Architecture
One idea was to have 4 specialized agents:
- Analyst (data only)
- Strategist (goals and trajectory)
- Coach (communication and tone)
- Integrator (synthesize into email)

**This is an idea, not a decision.** It might be overkill. Start simple.

### Data Layers for Scalability
Another idea was layered data:
- Context (always loaded): Who they are, goals
- Window (recent): Last 2-4 weeks
- Index (summaries): Compressed history
- Archive (old data): Only if needed

**Also just an idea.** Figure out what's actually needed.

### Google Ecosystem
User wants everything synced with their Google account:
- Google Sheets (in their Drive) for data
- Gmail for sending coaching emails
- No external services if possible

## The User's Situation (as of the conversation)

### Training
- Currently on a strength program (was Week 7)
- 4 training days per week
- Travel disrupts schedule every 2 weeks
- Goals include strength targets (squat, bench, etc.)

### Health
- Insulin resistant (carb timing matters)
- Golfer's elbow (affects pull-ups)
- Lost cardio fitness from sedentary work

### Life
- Works 14-16 hours/day in finance
- Travels Mon-Thu every 2 weeks
- Feels burned out, questions purpose
- Based in Spain

### Communication
- Direct, no fluff
- Data over motivation
- Switches between Spanish and English
- Hates inconsistency and pandering

## What Needs To Be Figured Out Together

1. **Sheet structure** - What does their actual program look like? What tabs are needed?
2. **Daily workflow** - What exactly do they input? What do they want in the email?
3. **Architecture** - Simple script? Multiple agents? Something else?
4. **Intelligence level** - How smart does the analysis need to be?
5. **Email frequency** - Daily? Only when something matters?
6. **Questions and suggestions** - When should the system ask vs just report?

## Starting Point

The user has:
- An existing Excel with their training program
- A Google account for Sheet + Gmail
- Claude API access (or will get it)

Next step: Understand their actual Excel structure and design from there.

---

*This context was compiled from a coaching conversation. The user explicitly said they want to build this together, not have it dictated to them.*
