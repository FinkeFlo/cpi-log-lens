"""Generate the synthetic CPI trace log used by MOCK mode (trace_sample.log).

All content is fictional: IFlow names are made up, hosts use example.com /
example.org (RFC 2606) and IPs come from the RFC 5737 documentation ranges.
The output is deterministic (fixed seed), so re-running the script produces
the same file.

Usage:
    python backend/mock/generate_sample.py [output_path] [lines]
"""
import random
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

SEED = 42
DEFAULT_LINES = 2000
START = datetime(2026, 1, 15, 8, 0, 0)

IFLOWS = [
    "Demo_Order_to_S4",
    "Demo_Invoice_Sync",
    "Demo_Customer_Replication",
    "Demo_Stock_Update_Webshop",
    "Demo_Payment_Status_Poll",
    "Demo_Delivery_Notification",
    "Demo_Pricing_Condition_Upload",
    "Demo_Employee_Master_Data",
    "Demo_Supplier_Onboarding",
    "Demo_Shipment_Tracking_Events",
    "Demo_Material_Master_Export",
    "Demo_Bank_Statement_Import",
]

LOGGERS = [
    "com.sap.esb.camel.route.policy.SingletonExchangeSelectionPolicy",
    "com.sap.esb.camel.route.policy.util.SchedulerReadyForMission",
    "com.sap.it.op.agent.mpl.MessageProcessingLogger",
    "com.sap.it.rt.adapter.http.common.HttpClientHelper",
    "com.sap.gateway.core.ip.component.odata.ODataProducer",
    "com.sap.it.script.engine.ScriptExecutor",
]

CATEGORIES = [
    "com.sap.esb.camel.quartz.camel.route.policy",
    "com.sap.it.op.agent.mpl",
    "com.sap.it.rt.adapter.http",
]

IP_PREFIXES = ["192.0.2.", "198.51.100.", "203.0.113."]

ENDPOINTS = [
    "https://erp.example.com/sap/opu/odata/sap/API_SALES_ORDER_SRV",
    "https://shop.example.org/api/v2/stock",
    "https://crm.example.com/api/customers",
    "https://payments.example.org/v1/status",
]

LEVEL_WEIGHTS = [("INFO", 70), ("DEBUG", 10), ("WARN", 12), ("ERROR", 8)]

STACKTRACE = [
    "\tat org.apache.camel.processor.errorhandler.RedeliveryErrorHandler.handleException(RedeliveryErrorHandler.java:512)",
    "\tat org.apache.camel.processor.Pipeline.process(Pipeline.java:163)",
    "\tat com.sap.it.rt.adapter.http.common.HttpClientHelper.execute(HttpClientHelper.java:214)",
    "\tat java.base/java.lang.Thread.run(Thread.java:840)",
]


def _mpl_id(rng: random.Random) -> str:
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_"
    return "AG" + "".join(rng.choice(alphabet) for _ in range(26))


def _thread(rng: random.Random, iflow: str) -> str:
    """Thread names in the three shapes db._extract_iflow() understands."""
    shape = rng.random()
    if shape < 0.6:
        return f"{rng.randint(1_700_000_000_000, 1_799_999_999_999)}-{iflow}_Worker-{rng.randint(1, 4)}"
    if shape < 0.85:
        return f"scheduler-{iflow}_Worker-1"
    return f"Camel ({iflow}) thread {rng.randint(1, 40)} - timer://{iflow}"


def _message(rng: random.Random, level: str) -> tuple[str, list[str]]:
    mpl = _mpl_id(rng)
    endpoint = rng.choice(ENDPOINTS)
    if level == "ERROR":
        status = rng.choice([401, 404, 500, 503])
        text = (f"Error while processing MPL {mpl}: HTTP call to {endpoint} "
                f"failed with status {status}")
        return text, STACKTRACE[: rng.randint(2, len(STACKTRACE))] if rng.random() < 0.4 else []
    if level == "WARN":
        return f"Retrying request to {endpoint} (attempt {rng.randint(1, 3)}/3) for MPL {mpl}", []
    if level == "DEBUG":
        return f"Exchange property SAP_MessageProcessingLogID={mpl}", []
    return rng.choice([
        f"[INFO] MPL: {mpl} ; Getting cluster lock with :{uuid.UUID(int=rng.getrandbits(128))}_CRON_{rng.randint(1000, 99999)}",
        f"Message processing started for MPL {mpl}",
        f"HTTP call to {endpoint} returned 200 in {rng.randint(20, 2500)} ms",
        "Timer execution Check for isWorkerReadyForTimerExecution : true ",
    ]), []


def generate(lines: int = DEFAULT_LINES, seed: int = SEED) -> list[str]:
    rng = random.Random(seed)
    levels = [lvl for lvl, _ in LEVEL_WEIGHTS]
    weights = [w for _, w in LEVEL_WEIGHTS]
    ts = START
    out: list[str] = []
    while len(out) < lines:
        ts += timedelta(seconds=rng.randint(0, 20))
        level = rng.choices(levels, weights)[0]
        iflow = rng.choice(IFLOWS)
        text, continuation = _message(rng, level)
        fields = [
            ts.strftime("%Y-%m-%d %H:%M:%S"), "+0000", level, rng.choice(LOGGERS),
            "anonymous", _thread(rng, iflow), rng.choice(CATEGORIES),
            "na", "na", "na", "na", text, "-",
            rng.choice(IP_PREFIXES) + str(rng.randint(1, 254)), str(rng.randint(1, 8)),
        ]
        out.append("#".join(fields))
        out.extend(continuation)
    return out[:lines]


def main() -> None:
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).with_name("trace_sample.log")
    count = int(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_LINES
    target.write_text("\n".join(generate(count)) + "\n", encoding="utf-8")
    print(f"wrote {count} lines to {target}")


if __name__ == "__main__":
    main()
