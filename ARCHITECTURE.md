# App Lifecycle
1. Load config + init logger
2. Create database instance
3. Create http server instance
4. Create sources according to config.yaml table, pass database and http server instance to them (so they can register endpoints + work with data)
5. Each source has name + type. Name is just for config and logging, type determines which source class gets initialized. name is from key, if type is not present, its assumed that name == type.
6. Inner config map of the source get passed to the source class to init itself
7. Create sinks according to config.yaml table. key is name, type determines which sink class gets initialzied. name is from key, if type is not present its assumed that name == type. pass database and http server instance to them (so they can registr endpoints + work with database)

# Event sources
1. They receive events by whatever means (expose endpoint on fastapi), short polling http resources, connect to web socket, ...
2. They can query database to check if they didn't process with event_id already
3. They enrich the events with additional data if necessary
4. They pass down the enriched event to database / queue

# Event sinks
1. They load events from database / queue according to matchers (* means all, abc.* means all prefixed with abs
2. They possibly use coalescer to coalesce events of the same type and same entity_id (http pop for example)
3. They can read internal state from databa and update it afterwards (http pop batch + confirmed procesed batch)
4. They send the events somewhere (SSE, webhook, websocket)... and possibly update internal db state

# Event Pipeline
1. Receive events from a data source & enrich them with additional data if necessary (within the data source)
2. Deduplicate by event_id so that we don't store duplicate events
3. Store them durably in a database
4. Coalesce events of the same type and same entity_id 
5. HTTP pop state maintains the last seen date_time


# Data Sources
- [ ] Google Drive
- [ ] Gmail
- [ ] Google Calendar
- [ ] Google Docs
- [ ] Faktury Online
- [ ] Fio Banka
- [ ] Asana
- [ ] Stats.fm
- [ ] Steam
- [ ] Chrome Web History

# Sinks
- [ ] Http Pop
- [x] Webhook
- [x] SSE

# Data Model

## Source
- id: int
- name: str
- type: str
- cursor: str

## Event
- id: int
- event_id: str
- source_id: int
- event_type: str
- entity_id: str
- created_at: datetime
- data: json

## Http Webhook Deliveries
- id: int
- event_id: int
- tries: int
- last_try: datetime
- created_at: datetime
- delivered: bool

## Http Pop Batch
- id: int
- created_at: datetime

## Http Pull Batch Events
- id: int
- event_id: int
- processed: bool