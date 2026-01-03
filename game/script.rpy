# script.rpy — Promptorium (Lambda+Gemini) STABLE NETWORK (curl) + SAFE TEXT RENDERING + DEBUG LOG

define e = Character("System")

default prompt_input = "Hello world"
default is_loading = False
default error_text = ""
default result = None

default debug_lines = []
default debug_enabled = True
default current_job_id = 0


# -------------------------
# CONFIG + NETWORK
# -------------------------
init python:
    import json
    import time
    import subprocess
    import platform

    # ✅ Keep your Lambda Function URL here
    LAMBDA_URL = "https://o2jsgo4xe3oax7znhsf6wf24me0anrxn.lambda-url.us-east-2.on.aws/"

    # ✅ Keep your token here
    APP_TOKEN = "promptorium_dev_token_9f3a1c7e82d44b9b"

    CURL_TIMEOUT = 30          # hard cap at OS level
    WATCHDOG_SECONDS = 45      # must be > CURL_TIMEOUT


    # ---- Safety: escape anything Ren'Py might treat as markup/interpolation ----
    def safe_text(s):
        """
        Ren'Py 'text' treats {...} as tags and [...] as interpolation.
        Escape them so JSON/logs never crash the renderer.
        """
        s = str(s)
        s = s.replace("{", "{{").replace("}", "}}")
        s = s.replace("[", "[[").replace("]", "]]")
        return s


    # ---- Logging helpers ----
    def _ui_log(msg):
        ts = time.strftime("%H:%M:%S")
        line = "({}) {}".format(ts, safe_text(msg))

        store.debug_lines.append(line)
        if len(store.debug_lines) > 250:
            store.debug_lines = store.debug_lines[-250:]

        try:
            renpy.log(line)
        except Exception:
            pass

        renpy.restart_interaction()

    def log(msg):
        if not getattr(store, "debug_enabled", True):
            return
        try:
            renpy.invoke_in_main_thread(_ui_log, msg)
        except Exception:
            try:
                renpy.log("[log-fallback] " + str(msg))
            except Exception:
                pass


    # ---- Curl-based POST (reliable hard timeout) ----
    def lambda_grade_prompt(user_prompt_text):
        log("Preparing request payload...")
        payload = {"prompt": user_prompt_text}
        payload_json = json.dumps(payload, ensure_ascii=False)

        # Build curl command
        # -sS : silent but show errors
        # --max-time : hard timeout (seconds)
        # -H headers
        # --data-binary : raw JSON body
        cmd = [
            "curl",
            "-sS",
            "--max-time", str(CURL_TIMEOUT),
            "-X", "POST",
            "-H", "Content-Type: application/json",
        ]

        if APP_TOKEN:
            cmd += ["-H", "x-app-token: {}".format(APP_TOKEN)]

        cmd += ["--data-binary", payload_json, LAMBDA_URL]

        log("Calling curl with hard timeout={}s...".format(CURL_TIMEOUT))
        log("Target: {}".format(LAMBDA_URL))

        # Prevent Windows from popping a console window
        creationflags = 0
        if platform.system().lower().startswith("win"):
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=CURL_TIMEOUT + 5,   # small cushion above curl's own cap
                creationflags=creationflags
            )
        except subprocess.TimeoutExpired:
            log("CURL TIMEOUT EXPIRED (python-level).")
            raise Exception("Hard timeout (curl). Network call did not complete.")
        except FileNotFoundError:
            log("curl not found on PATH.")
            raise Exception("curl not found. Install curl or add it to PATH.")
        except Exception as ex:
            log("subprocess.run exception: {}".format(str(ex)))
            raise Exception("Subprocess failed: {}".format(str(ex)))

        stdout = proc.stdout or ""
        stderr = proc.stderr or ""

        log("curl return code: {}".format(proc.returncode))
        if stderr.strip():
            log("curl stderr (first 240 chars): {}".format(stderr[:240].replace("\n", "\\n")))

        if proc.returncode != 0:
            # curl non-zero = network/TLS/DNS/etc.
            raise Exception("curl failed (code {}). stderr: {}".format(proc.returncode, stderr[:500]))

        log("Received bytes: {}".format(len(stdout)))

        # Parse JSON (or fallback)
        log("Parsing JSON...")
        try:
            parsed = json.loads(stdout)
            if isinstance(parsed, dict):
                log("JSON OK. Keys: {}".format(", ".join(parsed.keys())))
            else:
                log("JSON OK. Type: {}".format(type(parsed)))
            return parsed
        except Exception:
            log("JSON parse FAILED. Raw starts: {}".format(stdout[:160].replace("\n", "\\n")))
            return {
                "score": 0,
                "rubric": {"clarity": 0, "empathy": 0, "actionability": 0, "alignment_with_task": 0},
                "feedback": "Model did not return valid JSON. (UI rendered safely) Tighten model output to pure JSON.",
                "improvedAnswer": "",
                "raw": stdout[:4000],
            }


# -------------------------
# THREADING + WATCHDOG
# -------------------------
init python:
    def _finish_request(job_id, res_dict=None, err_str=""):
        if job_id != store.current_job_id:
            log("Ignoring late response from old job_id={} (current={})".format(job_id, store.current_job_id))
            return

        store.is_loading = False
        store.error_text = err_str or ""
        store.result = res_dict
        renpy.restart_interaction()

    def _worker_call_lambda(job_id, prompt_txt):
        log("Worker thread started (job_id={}).".format(job_id))
        try:
            res = lambda_grade_prompt(prompt_txt)
            log("Worker finished successfully (job_id={}).".format(job_id))
            renpy.invoke_in_main_thread(_finish_request, job_id, res, "")
        except Exception as ex:
            log("Worker failed (job_id={}): {}".format(job_id, str(ex)))
            renpy.invoke_in_main_thread(_finish_request, job_id, None, str(ex))

    def _watchdog_timer(job_id, start_time):
        log("Watchdog armed ({}s) for job_id={}.".format(WATCHDOG_SECONDS, job_id))
        while True:
            time.sleep(0.25)
            if job_id != store.current_job_id:
                return
            if not store.is_loading:
                return
            if time.time() - start_time > WATCHDOG_SECONDS:
                log("WATCHDOG FIRED (job_id={}): forcing UI to stop loading.".format(job_id))
                renpy.invoke_in_main_thread(
                    _finish_request,
                    job_id,
                    None,
                    "Watchdog timeout ({}s). Check debug log.".format(WATCHDOG_SECONDS)
                )
                return

    def submit_prompt_async():
        prompt_txt = store.prompt_input.strip()

        if not prompt_txt:
            store.error_text = "Please enter a prompt first."
            store.result = None
            store.is_loading = False
            renpy.restart_interaction()
            return

        store.current_job_id += 1
        job_id = store.current_job_id

        store.is_loading = True
        store.error_text = ""
        store.result = None

        store.debug_lines = []
        renpy.restart_interaction()

        log("Submit clicked.")
        log("Job id: {}".format(job_id))
        log("Prompt length: {}".format(len(prompt_txt)))

        start_time = time.time()
        renpy.invoke_in_thread(_worker_call_lambda, job_id, prompt_txt)
        renpy.invoke_in_thread(_watchdog_timer, job_id, start_time)

    def ping_thread():
        def _ping():
            log("Ping thread running...")
            time.sleep(0.5)
            log("Ping thread done ✅")
        renpy.invoke_in_thread(_ping)


# -------------------------
# UI
# -------------------------
transform blink:
    alpha 0.2
    linear 0.5 alpha 1.0
    linear 0.5 alpha 0.2
    repeat

screen promptorium_screen():
    tag menu

    frame:
        xalign 0.5
        yalign 0.5
        xsize 1050
        ysize 720
        padding (24, 24)

        vbox:
            spacing 12

            text "The Promptorium — Prompt Grader" size 30

            text "Enter a prompt:" size 18
            input value VariableInputValue("prompt_input") length 800 xsize 980

            hbox:
                spacing 12
                textbutton "Grade Prompt" action Function(submit_prompt_async) sensitive (not is_loading)
                textbutton "Ping Thread" action Function(ping_thread)

                if is_loading:
                    text "Calling Lambda..." size 16
                    text "..." size 32 at blink

            if error_text:
                text ("Error: " + error_text) color "#ff4444" size 18 substitute False

            if result:
                $ pretty = safe_text(json.dumps(result, indent=2, ensure_ascii=False))
                text "Result:" size 18
                viewport:
                    xsize 1000
                    ysize 210
                    scrollbars "vertical"
                    draggable True
                    mousewheel True
                    text pretty size 14 substitute False

            text "Debug Log:" size 18
            viewport:
                xsize 1000
                ysize 240
                scrollbars "vertical"
                draggable True
                mousewheel True
                vbox:
                    spacing 2
                    for line in debug_lines:
                        text line size 14 substitute False


# -------------------------
# GAME FLOW
# -------------------------
label start:
    scene black
    e "Welcome to The Promptorium."
    call screen promptorium_screen
    e "Done."
    return
