"""Run the existing missing-blob probe with a single no-thin update push."""
import get_push_modified_probe as probe


def main():
    original = probe.run_git
    def run(local, stage, *args):
        if stage == 'update_branch':
            if not args or args[0] != 'push':
                raise ValueError('unexpected_push')
            args = (args[0], '--no-thin', *args[1:])
        return original(local, stage, *args)
    probe.run_git = run
    try:
        probe.main(hydrate=False)
    finally:
        probe.run_git = original


if __name__ == '__main__':
    try:
        main()
    except Exception:
        raise SystemExit('probe_failed') from None
