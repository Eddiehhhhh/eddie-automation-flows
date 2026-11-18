#!/usr/bin/env python3
"""Temporary, read-only access probe. Never emits private response content."""
import json, os, urllib.request, urllib.error

def main():
    if (os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
            or os.environ.get("GITHUB_REF") != "refs/heads/codex/workout-readonly-probe-20261118"):
        print("read_only_probe_refused")
        return 1
    key=os.environ.get("NOTION_API_KEY", "")
    workout=os.environ.get("WORKOUT_PROBE_WORKOUT_ID", "")
    diary=os.environ.get("WORKOUT_PROBE_DIARY_ID", "")
    expected=os.environ.get("WORKOUT_PROBE_EXPECTED_BOT_ID", "")
    if not all((key,workout,diary,expected)):
        print("read_only_probe_configuration_missing")
        return 1
    def get(path):
        req=urllib.request.Request("https://api.notion.com/v1"+path,headers={"Authorization":"Bearer "+key,"Notion-Version":"2022-06-28"},method="GET")
        try:
            with urllib.request.urlopen(req,timeout=20) as response:
                return response.status,json.load(response)
        except urllib.error.HTTPError as exc:
            return exc.code,{}
        except Exception:
            return 0,{}
    identity_status,identity=get("/users/me")
    workout_status,workout_db=get("/databases/"+workout)
    diary_status,diary_db=get("/databases/"+diary)
    print(json.dumps({"read_only":True,"identity_http_status":identity_status,
        "same_as_expected_connection":identity_status==200 and identity.get("id")==expected,
        "workout_http_status":workout_status,"diary_http_status":diary_status,
        "workout_identity_verified":workout_status==200 and workout_db.get("id")==workout,
        "diary_identity_verified":diary_status==200 and diary_db.get("id")==diary,
        "relation_target_verified":any(p.get("type")=="relation" and (p.get("relation") or {}).get("database_id")==diary for p in (workout_db.get("properties") or {}).values())},sort_keys=True))
    return 0

if __name__=="__main__":
    try:
        raise SystemExit(main())
    except Exception:
        print("read_only_probe_failed")
        raise SystemExit(1)
