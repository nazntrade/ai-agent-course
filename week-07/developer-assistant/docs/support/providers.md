# Providers and authentication

Developer Assistant has two explicitly selected providers: Local and DeepSeek.
The selected provider never silently falls back to the other one.
Authentication, авторизация, ключ, key and credentials are provider-specific.
Do not paste any API key, token or password into a chat or support ticket.

## Credential loading and independent processes

DeepSeek credentials are read from the DEEPSEEK_API_KEY process environment;
Local credentials are read from LOCAL_LLM_API_KEY. assistant/config.py first
loads the module .env with load_dotenv(override=False), unless environment-file
loading is explicitly disabled. An already defined process environment variable
takes precedence over .env, including an already defined empty value.
local-data/settings.json contains nonsecret URL/model/runtime and generation
parameters, not API keys. Adding a key there does not configure a credential.
Inspect credentials privately in the actual .env/process environment source;
never share their values, screenshots or full configuration in support.

Main and Support are independent processes. Each loads Settings once when its
application starts. First identify where the failing request was made: the main
chat/review or Support Desk. Restarting Main can reuse an existing Support
process and does not refresh that Support process's loaded environment.
If reload is needed, restart only the affected identified process, using its
normal launch procedure; do not stop an unknown listener or other services.
Also check the launch environment precedence: editing .env cannot override an
already defined variable inherited by the new process. A replacement or restart
alone is not evidence that the intended credential was loaded or accepted.
Confirm the selected provider and the next safe error/success response. Do not
infer relative frequency or a proven configuration cause from an error alone.

## DeepSeek configuration and authentication

DeepSeek uses its official HTTPS API. An explicit invalid_key authentication
error indicates that the supplied credential was not accepted by that provider.
It does not identify which credential source supplied it or prove why it was
rejected. Refer to the documented credential loading and process lifetime above.
If a key was already replaced, check the affected process and its real loading
source/precedence privately before repeating replacement as the only action.
Ask what was changed and which process was restarted when those facts are
missing. Retain the safe error code, selected provider and observation time.
If the issue remains unresolved after these checks, escalate with safe metadata
and attempted steps, never the credential. No assistant can inspect your account
or guarantee a diagnosis from synthetic demo context.

## Local availability and connection

Local uses a loopback OpenAI-compatible endpoint and a configured model alias.
Connection refused means no connection was accepted at that endpoint. Check
whether the configured server is running, whether its port and model alias
match the application configuration, and whether startup has completed.
Changing a DeepSeek credential does not start a Local server. The application
can start only its explicitly configured local runtime, and does not kill an
unknown service occupying its port. A borrowed local server is not owned or
stopped by Support. Long model loading is not by itself a failure.
Ask for the selected provider and a safe error code if those facts are missing.
