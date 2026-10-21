"""Credential-free gate for exact reviewed route selections.

Only irreversible authorization commitments are public, not private members,
source paths or plans. This is the first gate, never permission for a business
write. The original complete private registration check runs after loading the
code-pinned private capsule and before Get/Notion effects.
"""
import hashlib
import os
import re

CONTRACT = 'get-route-selection-1'
MODES = ('canonical', 'historical-capture', 'historical-apply', 'property-apply')
# Each commitment was derived only after the full original preflight passed.
APPROVED_COMMITMENTS = frozenset(('0d821367229be017612eae7599ad77fce226bf8e1a932ee5c5453c6f54128a8e', '1b86474d93bb434cf8f12bab08b70c8de6692bcf41b5f7ba70ff4c94a2302fb2', '1d687d6ea5d1a734d82326e0a971714d9d9e86b3a5fe0189575079bf73c16bae', '2b6eba52727716851f31bd20af09dbdfbd12054bbd45fe610e9ab8899340145c', '2ddfc9225af41d07075e86c71cf80d56d832ae530925685abccb12cab52fab4a', '4cc9b0827bea18cccb6f1737d4424dc0e143deb790a140efacce5849bb8f3878', '60302b13ee3d6c5112dba3a7ffc8c7037c92fde2d88c45d93cda6b7aedd4e0ef', '6ba3aa0a6176bd744f17c35a4289a686db2e78b7ef160f17a12e6aede3a4d57b', '8fffdb7a55a795ad2888dfa879adfc358a156cd75c93d8481ac9b42d1f664b9f', '9be806ee38ea52143b12d00a4727e73ec57942b18498e9ca4425378dd457dc66', 'a167346ef108dbf60d0566103e325dc53520f4527610090b962ed7ee186094d0', 'b25478145bfba2455520ab2d8d1ab9ca66245ec1ea534e941432f009044e897a', 'c3eedb3844ccf2cdabae5871f4fc73315f047bac149d28d2cbbaa03bd3118534', 'ccb4c6d15f2997f52f2e3cf8f8f54b2805a345f38179dc1f203154d029d05c39', 'd344c899bc23313d6e46f48ee1e012362a017606c0dcc302a17ff963de9c1d5e', 'df7d03764a1530250dd076cb255fe71a53a0ea80f373718e8e83aee2e76c03d0', 'e58fe3c0d1eba7e91c60177c5a1468abbea346e310a4e62baeb47ba07cf8685c', 'eb4b92ebe5f241d35c4b595786522ffed841d9ecffe954ef09d962387ff067f8', 'ece9491f699ce6660c825f75482ed2d8c918a302962807ffc66cdc5db2615ded', 'ed4c115bcb1d19f51d599bea0cd8fc6fbcc421d54baf99e9a1ad032172d1d0d9', 'f4abb74203707fe882a630d1a449922e5ffdbb71b3fc4af64d2b87b9ec2ebb0c', 'f519609f2d6618208a0b1c1b42e174367bc131742c464e76ca75ca7b802b2256', 'f5ced4106935cb6dda84ff9b4f903f8af53fffee9b9187067d6ca17cee9ae61f', 'f75eab1a8b5bd04ddf40974b488e86b0e4f8bcba1c6387c9ca9b75c83b7daf27'))

class SelectionError(ValueError): pass

def fail(): raise SelectionError('notion_route_blocked') from None

def commitment(mode, expected):
    return hashlib.sha256((CONTRACT+'\0'+mode+'\0'+expected).encode()).hexdigest()

def preflight(event, apply, mode, expected):
    if event not in ('schedule','workflow_dispatch') or type(apply) is not bool: fail()
    if mode not in MODES or not isinstance(expected,str): fail()
    if mode=='canonical':
        if expected or event=='schedule' and not apply: fail()
        return True
    if (event!='workflow_dispatch' or not apply or not re.fullmatch('[0-9a-f]{64}',expected)
            or commitment(mode,expected) not in APPROVED_COMMITMENTS): fail()
    return True

def selection():
    apply=os.environ.get('APPLY')
    if apply not in ('true','false'): fail()
    return preflight(os.environ.get('GITHUB_EVENT_NAME'),apply=='true',
                     os.environ.get('NOTION_MODE','canonical'),os.environ.get('NOTION_INPUT_SHA256',''))

def main():
    try:
        selection()
        print('notion_route_selection_verified',flush=True);return 0
    except Exception:
        print('notion_route_blocked',flush=True);return 1

if __name__=='__main__':raise SystemExit(main())
