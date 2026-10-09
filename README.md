# Illuminate Conversational Intelligence API

Backend for natural-language and governed analytics over Anthology Illuminate's CDM data in
Snowflake. A single FastAPI app, run on AWS Lambda, serves:

- **Semantic queries**: callers send a query contract (metrics, measures, dimensions, filters); the
  semantic layer compiles it to Snowflake SQL from governed dataset and metric definitions.
- **Chat**: a Bedrock Converse tool loop that answers questions through the same semantic layer,
  falling back to labelled, guarded freehand SQL.
- **Tenant overlays**: per-tenant, versioned admin overrides of measures, filters and metric defaults.
- **Data dictionary**: an authenticated proxy to the Blackboard data dictionary, and an admin-only table preview.

```
Client --HTTPS + Cognito ID token--> Lambda Function URL (RESPONSE_STREAM)
  --> Lambda Web Adapter --> uvicorn / FastAPI (lambda_handler.py)
        |-- semantic_layer/ compile --> execution guard --> Snowflake
        |-- chat_engine.py (Bedrock) --> semantic tools --> execution guard --> Snowflake
        |-- overlay_store.py / conversation_store.py --> DynamoDB
        '-- data dictionary proxy --> us.data.api.blackboard.com
```

## Tech stack

| Layer | Technology |
|-------|------------|
| API | FastAPI on AWS Lambda (Python 3.11) via Lambda Web Adapter, SSE streaming |
| Auth | Amazon Cognito ID tokens |
| LLM | Claude Sonnet 4.6 on Amazon Bedrock (Converse API) |
| Semantic layer | YAML definitions in `canonical/`, compiled with sqlglot and sandboxed Jinja |
| Storage | DynamoDB (conversations, overlays), Secrets Manager (Snowflake credentials) |
| Warehouse | Snowflake |
| Infrastructure | AWS CDK (TypeScript), two stacks |

## Quick start

```bash
# Tests (no AWS, Bedrock or Snowflake needed)
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-lambda.txt -r requirements-dev.txt
python -m pytest

# Deploy
cp .env.example .env    # Snowflake credentials and the initial Cognito user
cd cdk && npm install
npx cdk deploy --all -c environment=dev
```

## Repository layout

```
lambda_handler.py       FastAPI app: routes, auth, overlay application, PII scrub
chat_engine.py          Bedrock Converse tool loop
conversation_store.py   DynamoDB conversation history
overlay_store.py        DynamoDB tenant overlays, versioned
snowflake_client.py     Snowflake connection and the execution guard
semantic_layer/         Catalog loader, schema, compiler, validator, overlays, chat tools, prompt
canonical/datasets/     Dataset definitions, one YAML file each, grouped by domain
canonical/metrics/      Metric definitions, grouped by domain
cdk/                    CDK app: IlluminateBase-<env> and IlluminateApi-<env>
scripts/                build_dictionary_snapshot.py (CDM dictionary fixtures), set-snowflake-secret.sh
tests/                  pytest suite
run.sh                  Lambda Web Adapter entry point
```

## Documentation

- [Architecture](docs/ARCHITECTURE.md): components, request flow, overlays, conversations, PII model
- [API reference](docs/API.md): every route, with auth, request, response and errors
- [Development](docs/DEVELOPMENT.md): setup, running locally, tests, adding datasets, metrics and overlays
- [Deployment](docs/DEPLOYMENT.md): CDK stacks, environment variables, secrets
- [Product spec](SPEC.md): original product requirements
