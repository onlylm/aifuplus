"""Client for the Pro20x Automate Open API V4 (see pro20x-automate-api-v4.md).

Documented and used here:
  POST /tasks                  submit (idempotent on body.clientOrderId)
  GET  /tasks?clientOrderId=   exact lookup by client order id
  GET  /tasks/{taskId}         task detail
  GET  /config                 plans / regions / paymentSources
Auth header: X-Automate-Key.  Errors: {success:false, code, message, requestId}.

NOT specified by the document (so parsed tolerantly, never assumed successful):
  * the exact JSON shape of the task object returned by POST /tasks and GET /tasks/{id}
  * the exact JSON shape of the GET /tasks list response
"""
import json
import uuid

TASK_SUCCESS = {"succeeded", "success"}
TASK_FAILED = {"failed", "cancelled", "canceled"}
TASK_MANUAL = {"manual_review"}
TASK_PROGRESS = {"queued", "running", "captcha_required", "pending", "processing"}


class UpstreamTransportError(Exception):
    """Timeout / connection failure: the request may or may not have been processed."""


class UpstreamResponse:
    def __init__(self, http_status, data, raw):
        self.http_status, self.data, self.raw = http_status, data, raw

    @property
    def ok(self):
        return 200 <= self.http_status < 300 and isinstance(self.data, dict) and self.data.get("success") is not False

    @property
    def error_code(self):
        return (self.data or {}).get("code", "") if isinstance(self.data, dict) else ""

    @property
    def error_message(self):
        return (self.data or {}).get("message", "") if isinstance(self.data, dict) else ""


def default_transport(method, url, headers, body, timeout):
    import requests
    try:
        r = requests.request(method, url, headers=headers, data=body, timeout=timeout)
    except requests.RequestException as e:
        raise UpstreamTransportError(str(e))
    return r.status_code, r.text


def parse_task(data):
    """Return {id, status, error_code, error_message} from a task-like payload, or None."""
    if not isinstance(data, dict):
        return None
    t = data
    for k in ("task", "data", "order", "job"):
        if isinstance(data.get(k), dict):
            t = data[k]
            break
    tid = t.get("id") or t.get("taskId") or t.get("task_id") or ""
    status = str(t.get("status") or "").lower()
    err = t.get("error") if isinstance(t.get("error"), dict) else {}
    return {
        "id": str(tid),
        "status": status,
        "error_code": str(err.get("code") or t.get("errorCode") or ""),
        "error_message": str(err.get("message") or t.get("errorMessage") or ""),
    }


def parse_task_list(data):
    """Return list of task dicts from a list response, or None when the shape is unrecognized."""
    items = None
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        for k in ("tasks", "items", "list", "data", "results"):
            v = data.get(k)
            if isinstance(v, list):
                items = v
                break
            if isinstance(v, dict):
                for kk in ("tasks", "items", "list"):
                    if isinstance(v.get(kk), list):
                        items = v[kk]
                        break
                if items is not None:
                    break
    if items is None:
        return None
    return [parse_task(i) for i in items if isinstance(i, dict)]


def parse_account_check_job(data):
    """Return {id, status, result} from an account-check job payload, or None."""
    if not isinstance(data, dict):
        return None
    job = data.get("job") if isinstance(data.get("job"), dict) else data
    if not isinstance(job, dict):
        return None
    jid = job.get("id") or job.get("jobId") or job.get("job_id") or ""
    status = str(job.get("status") or "").lower()
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    if not jid and not status and not result:
        return None
    return {"id": str(jid), "status": status, "result": result}


class Pro20xClient:
    def __init__(self, base_url, api_key, transport=None, timeout=20):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.transport = transport or default_transport
        self.timeout = timeout

    def _req(self, method, path, body=None, query=""):
        headers = {"X-Automate-Key": self.api_key, "Content-Type": "application/json",
                   "X-Request-Id": uuid.uuid4().hex}
        payload = json.dumps(body, ensure_ascii=False) if body is not None else None
        status, text = self.transport(method, self.base_url + path + query, headers, payload, self.timeout)
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        return UpstreamResponse(status, data, text)

    def config(self):
        return self._req("GET", "/config")

    def submit(self, body):
        return self._req("POST", "/tasks", body)

    def get_task(self, task_id):
        from urllib.parse import quote
        return self._req("GET", "/tasks/" + quote(task_id, safe=""))

    def find_by_client_order_id(self, client_order_id):
        from urllib.parse import quote
        return self._req("GET", "/tasks", query="?status=all&page=1&pageSize=20&clientOrderId=" + quote(client_order_id, safe=""))

    def account_check(self, body):
        return self._req("POST", "/account-check", body)

    def get_account_check_job(self, job_id):
        from urllib.parse import quote
        return self._req("GET", "/account-check/jobs/" + quote(job_id, safe=""))
