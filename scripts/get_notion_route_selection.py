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
APPROVED_COMMITMENTS = frozenset(('f357e8b451b3ca761a096f84a9e423732edd2ce2efe779fd25c8396844e7e73d', '2ff7e4d85824e52b90a40fac575076b05025fd8ac02264c2c86dc461001ddf54', 'c1086c8567d9f43c1deed972e8a64fec3af2ce0716ec6368a4aadbfbd4bef62f', '67b35d5aa2fd9a8edb3afb8516799bd7e48623529bcc05b8ab555c561a035ed7', 'df00f7a114513866d2496bdaab1587f1288ae82858eb407f2c7962bbba5de5ca', '856e657e59adeef512cc031c1e296acd164f8baccf6ecdedcb9158c5d4c09b10', '40fac1ded29186c05af5b1d3f96a6f5e184894aeacd3991ad31fa20eb5e99de8', '54ca167e253fab2fb5c8be14ba66815192472cb68d7bd78a793d062396f7bfde', '0536ef6bebf1ee0828e66d884a860b55fd10a5bd9cd98f6bb0816ed944993d44', '0d821367229be017612eae7599ad77fce226bf8e1a932ee5c5453c6f54128a8e', '1b5c1b04900951b4e2ed22a1d518969b166422002317239f68dd8a0712a92198', '1b86474d93bb434cf8f12bab08b70c8de6692bcf41b5f7ba70ff4c94a2302fb2', '1c5676040f0a9f744e783d81a27bb10f1b56dc19c8ceb24a2ed484744519ab76', '1d687d6ea5d1a734d82326e0a971714d9d9e86b3a5fe0189575079bf73c16bae', '2b6eba52727716851f31bd20af09dbdfbd12054bbd45fe610e9ab8899340145c', '2b79f6e7d30e6388eb9d3b2fa2fe220b3aa41a441505b5c8a29e9a4c904d5c79', '2ddfc9225af41d07075e86c71cf80d56d832ae530925685abccb12cab52fab4a', '336a34adc03bd7cf20b9fa0eb2bdfbec424e0098098e57f0596b46582e036341', '379e6bc29ca578c687d0e439c65ba814d7fc05a263d4ff85478521aae43b1fa0', '46ace3bbd4e410ed0092e9faa4060506f57cffc2a45b4b164bd1f28ebb85d61e', '4b6845ee3c325dcddeaa3dd5864085162c45b99c11e16b66e78d7d61ad2c223c', '4cc9b0827bea18cccb6f1737d4424dc0e143deb790a140efacce5849bb8f3878', '55f5dae6ff4ea6d831b0b6b4b77a5a0c3325824fbdd59250a90f4124f0631bca', '5b56d8fc965a573fd6daa6b5302271ac5595266d936b97a299ec36aaea08b9da', '5dc3d3ecf550bdf5fce396cf4cc32c32e820e9c9e6c314f40e53495c3f76c962', '5e1ba8d67199b40b3f0b54feb3732740a1c17912e9970ab0e4ca24869ea0ac23', '60302b13ee3d6c5112dba3a7ffc8c7037c92fde2d88c45d93cda6b7aedd4e0ef', '690b971cde281b536ef500bdd580736884f4e62591fb2f9effeb3b6f8cbeaf87', '6ba3aa0a6176bd744f17c35a4289a686db2e78b7ef160f17a12e6aede3a4d57b', '6e3ca49b42412bf787ce5499b928c30ca7168e22db49c409324986b8af16a2e7', '76abe5f2d46bdfde45e4a9385283583eb5864db1e929fddd8892c58af7af0cb1', '7ceca3b62e723acf9291f45665c75f4f162fd42c9cd7158aa5499e016dbedb09', '84da1eac84484318fa938504c57f79b889a2641d5945c7723f1c0606f6106544', '87f170606addaf2289706e9387accb58dded9164b04708d642696f98cfc1f88e', '8f938f86c7cca1cf40baef06ff9249ec960fbccc4abe57512556967545c54f78', '8fffdb7a55a795ad2888dfa879adfc358a156cd75c93d8481ac9b42d1f664b9f', '9be806ee38ea52143b12d00a4727e73ec57942b18498e9ca4425378dd457dc66', 'a167346ef108dbf60d0566103e325dc53520f4527610090b962ed7ee186094d0', 'b25478145bfba2455520ab2d8d1ab9ca66245ec1ea534e941432f009044e897a', 'ba3888ff17e602679162dfac2c63b2bc9cce3a2b583ab7d35bdfe7129894a0ae', 'c3eedb3844ccf2cdabae5871f4fc73315f047bac149d28d2cbbaa03bd3118534', 'ccb4c6d15f2997f52f2e3cf8f8f54b2805a345f38179dc1f203154d029d05c39', 'd344c899bc23313d6e46f48ee1e012362a017606c0dcc302a17ff963de9c1d5e', 'da102c545cc0f813cbc35c7a0d65efe189a79e0ef64244f92baa287788dd40d4', 'df7d03764a1530250dd076cb255fe71a53a0ea80f373718e8e83aee2e76c03d0', 'e1d0135e6588780ec935fe231d853c45338de6b8be142a700c3dd13c424b9e99', 'e492ab922168b851607a3e6f90ce5597f0757eb29c7df713681247819c07c7f8', 'e58fe3c0d1eba7e91c60177c5a1468abbea346e310a4e62baeb47ba07cf8685c', 'eb4b92ebe5f241d35c4b595786522ffed841d9ecffe954ef09d962387ff067f8', 'ece9491f699ce6660c825f75482ed2d8c918a302962807ffc66cdc5db2615ded', 'ed4c115bcb1d19f51d599bea0cd8fc6fbcc421d54baf99e9a1ad032172d1d0d9', 'f4abb74203707fe882a630d1a449922e5ffdbb71b3fc4af64d2b87b9ec2ebb0c', 'f519609f2d6618208a0b1c1b42e174367bc131742c464e76ca75ca7b802b2256', 'f5ced4106935cb6dda84ff9b4f903f8af53fffee9b9187067d6ca17cee9ae61f', 'f75eab1a8b5bd04ddf40974b488e86b0e4f8bcba1c6387c9ca9b75c83b7daf27', 'fcfd0e98baba1c5c73bb1b9753b6b9746d32de6d9e71af8ef06ab7c4cd4f94d1'))

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
