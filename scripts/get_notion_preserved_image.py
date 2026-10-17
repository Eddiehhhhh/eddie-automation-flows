"""Fixed image sample: exact committed source plus fresh existing-block evidence.

No upload, writable image identity or source/state mutation. The exact source
binding is independently fixed by the retained three-way historical proof.
"""
from copy import deepcopy
from urllib.parse import urlsplit
import get_notion_executor as e
import get_notion_image_projection as projection

TEMPLATE={'asset_blob': 'b79101f15a1e36730146b91f126c353f43ed4212', 'asset_bytes': 191784, 'asset_path_sha256': '8b92db0e5ce5d1ba4a9bec48afa4393f202e9d353ca6fb3772e4920aa59329de', 'asset_sha256': '6370d698c83f61c00bbfc4a75ed2fba67c40b4e517663e34ae3c7ffe72ec4558', 'candidate_roots': 19, 'candidate_semantic_sha256': '5168a8d1c99e32bf576d32254d6774c68865140253180e5629df9e00201b3b8b', 'contract': 'get-notion-preserved-image-text-proof-1', 'historical_evidence_sha256': '47e205a6ac771aa763943eab813d118b703ce4b20d9382cf306a4945cdf073f0', 'historical_readback_sha256': '42ad5a50314346707d85823c4f473a6eded0f5dc1b274a3e7e8de17699555ad0', 'image_line_sha256': 'a44edf755418393d301ed7ed42bd2cc60940ef0b79dad990bd40570cdc5a341c', 'note_id_sha256': '1b05800d685ea1add4c7e3abd6a1a5f471a87087dd55a6bfee612d9562280240', 'page_id_sha256': 'e5b9feb9ef5c24c9636bbaee769dcced53828c929615b7305c83ce6fb032246d', 'prepared_markdown_sha256': 'ce949cefd243c22c466b283ca591d34f8bab435748876e7e934ad5908a42e678', 'projection_sha256': 'fbdd6a775658ff52709cb825927ac6141ba2acccb148f8570e5f3fb050e957c4', 'proof_sha256': '63032a4a02c7de6930634255825c78e04f12de224d2ffa590b214ddee25d1e15', 'reference_sha256': '6bf6c4770b609f697ade26233e45cf096192b860b3761b6ce7a632f1b44350f0', 'source_binding_sha256': 'b2b1b0b3008aee968561b9ef6b16f8bc2fc28de430b42e28d1cc3985f2ec7c0e', 'source_blob': '941a7708dede1e0c89b213199bb3da9d5fe90e02', 'source_commit': 'cbcea1d0de84e106825146f39e1db726e06ace49', 'source_path_sha256': '9ec63e661cd848638bd8c687f7df2ddbc2591aff481477de14aa764976769177', 'source_sha256': '4ae07f389da46aae7085ce88c761e4007b8e7f9e60b605f6530648c61e4ca071', 'transform': 'single-bound-wiki-image-to-plain-paragraph-1', 'validation': 'prepared-readback-projected-logical-1'}
TEMPLATE_SHA='d930fb19d085087187a1b11266552c92e716a5523c988f28f4a55b522e06c949'

IMAGE_FAILURES=frozenset(('asset_source_changed','asset_target_changed','asset_bytes_mismatch','asset_host_unsupported','asset_source_type_unsupported','asset_download_budget','asset_byte_budget','asset_time_budget','asset_transport_failed','asset_http_failed','asset_caption_invalid','asset_caption_changed','asset_snapshot_invalid','asset_image_count','asset_parent_changed','asset_block_invalid','asset_block_changed','asset_page_changed','asset_media_mismatch','asset_changed','image_reference_changed','missing_asset','asset_binding_invalid'))


def source_binding(publication,ident,page_id):
    if e.p.digest(TEMPLATE)!=TEMPLATE_SHA or e.p.digest(ident)!=TEMPLATE['note_id_sha256'] or e.p.digest(page_id)!=TEMPLATE['page_id_sha256']:e.fail('missing_asset')
    row=publication.descriptor['records'].get(ident)
    if row is None or e.p.digest(row['path'])!=TEMPLATE['source_path_sha256']:e.fail('missing_asset')
    data=publication.read(ident);image=projection.source_image(data,ident,row['path'])
    binding={'contract':projection.CONTRACT,'note_id':ident,'source_path':row['path'],
        'reference':image['reference'],'asset_path':image['asset_path'],
        'asset_blob':TEMPLATE['asset_blob'],'asset_sha256':TEMPLATE['asset_sha256'],
        'asset_bytes':TEMPLATE['asset_bytes'],'image_line_sha256':image['image_line_sha256']}
    projection.check_binding(binding,TEMPLATE['source_binding_sha256'])
    projection.project_image_source(data,ident,row['path'],publication.descriptor['content_tree'],binding,TEMPLATE['source_binding_sha256'])
    return binding


def initial_proposal(publication,ident,snapshot,data_source_id,remote):
    row=publication.descriptor['records'][ident];data=publication.read(ident)
    proof=projection.reconstruct_proof(TEMPLATE,TEMPLATE_SHA,note_id=ident,page_id=snapshot['page_id'],source_path=row['path'],source_data=data,content_tree=publication.descriptor['content_tree'])
    old=remote.check_source_budget(proof['source']['commit']).get(row['path'])
    if old is None or old[:3]!=('100644','blob',proof['source']['blob']) or row['blob']!=proof['source']['blob']:e.fail('source_changed')
    return projection.propose_from_git(proof,TEMPLATE['proof_sha256'],data,publication.descriptor['content_tree'],snapshot,data_source_id)

class Adapter:
    """Each native read is independently bound; only fixed image ID is eligible."""
    def __init__(self,publication,request,asset_get=None):
        import get_notion_existing_image as existing
        self.existing=existing;self.publication=publication;self.request=request
        self.asset_get=existing.AssetGet() if asset_get is None else asset_get
        self.bindings={};self.failures={};self._downloads=0;self.host='not_observed'
    @property
    def downloads(self):return getattr(self.asset_get,'downloads',self._downloads)
    def report(self):return {'verified':len(self.bindings),'failures':{reason:list(self.failures.values()).count(reason) for reason in set(self.failures.values())},'host':self.host}
    def __call__(self,snapshot,ident):
        if e.p.digest(ident)!=TEMPLATE['note_id_sha256']:return snapshot
        self.bindings.pop(ident,None)
        try:
            images=[r for r in snapshot['recursive_blocks'] if r.get('type')=='image']
            host=urlsplit(images[0]['payload']['file']['url']).hostname if len(images)==1 else None
            self.host={'s3.us-west-2.amazonaws.com':'exact_known_s3','prod-files-secure.s3.us-west-2.amazonaws.com':'exact_prod_files_secure'}.get(host,'other')
        except Exception:self.host='other'
        try:
            sb=source_binding(self.publication,ident,snapshot.get('page_id'))
            result=self.existing.bind_existing_image(snapshot,source_binding=sb,
                expected_source_sha256=TEMPLATE['source_binding_sha256'],
                source_commit=self.publication.descriptor['commit'],
                content_tree=self.publication.descriptor['content_tree'],
                request=self.request,asset_get=self.asset_get)
            self._downloads+=result['asset_downloads']
            self.bindings[ident]={'source_binding':sb,'source_binding_sha256':TEMPLATE['source_binding_sha256'],
                'native_binding':result['binding']}
            self.failures.pop(ident,None)
            return result['snapshot']
        except Exception as exc:
            reason=str(exc)
            self._downloads+=getattr(exc,'asset_downloads',0)
            if reason in ('asset_permission_denied','permission_denied'):
                exc.asset_downloads=self.downloads
                raise
            # A missing/changed asset is a record conflict, not authority to
            # replace/omit the original native image. The raw shape stays blocked.

            self.failures[ident]=reason if reason in IMAGE_FAILURES else 'missing_asset'
            return snapshot
