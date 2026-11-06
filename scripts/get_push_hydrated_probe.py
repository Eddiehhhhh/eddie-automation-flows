"""Run the fixed modified-blob probe after verified old-blob hydration."""
import get_push_modified_probe as probe


if __name__ == '__main__':
    try:
        probe.main(hydrate=True)
    except Exception:
        raise SystemExit('probe_failed') from None
