"""Sample-fitted calibration, never code-selected business thresholds."""
import inspect
import math
from app.errors import DomainError

def fit_calibration(samples):
    groups={label:[raw for raw,rating in samples if rating==label] for label in ('cold','warm','hot')}
    if any(not groups[label] for label in groups): raise DomainError('CALIBRATION_INVALID','Cold, warm and hot labels required',409)
    b=max(groups['cold']); u=min(groups['hot'])
    if not b<u or max(groups['cold'])>=min(groups['warm']) or max(groups['warm'])>=min(groups['hot']):
        raise DomainError('CALIBRATION_OVERLAP','Human groups overlap; puzzle requires review',409)
    # Fit a power transform to the warm sample center. The target ordinal center
    # is the middle of the supplied three ordered human categories.
    center=sum(groups['warm'])/len(groups['warm']); ratio=(center-b)/(u-b)
    gamma=math.log(.5)/math.log(ratio)
    if not all(math.isfinite(v) for v in (b,u,gamma)) or gamma<=0: raise DomainError('CALIBRATION_INVALID','Invalid fitted calibration',409)
    return {'b':b,'u':u,'gamma':gamma,'method':'ordinal-center-fit/1','sample_count':len(samples)}

async def evaluate(scorer,puzzle,body):
    if getattr(scorer,'manifest_id',None)!=body['manifest_id'] or not callable(getattr(scorer,'embeddings',None)):
        raise DomainError('SCORER_NOT_READY','Matching real embedding provider required',503)
    texts=[puzzle['word']]+[p['guess'] for p in body['pairs']]
    vectors=[]
    for offset in range(0,len(texts),32):
        result=scorer.embeddings(texts[offset:offset+32])
        vectors.extend(await result if inspect.isawaitable(result) else result)
    dimension=getattr(scorer,'dimension',None)
    if not isinstance(dimension,int) or len(vectors)!=len(texts) or any(len(v)!=dimension or any(not math.isfinite(x) for x in v) or abs(sum(x*x for x in v)-1)>.002 for v in vectors):
        raise DomainError('SCORER_RESPONSE_INVALID','Invalid normalized embedding vectors',503)
    binding=puzzle.get('calibration_binding')
    if binding and (binding['dimension']!=dimension or binding['manifest_id']!=body['manifest_id'] or binding['manifest_sha256']!=getattr(scorer,'manifest_sha256',None) or any(abs(a-b)>1e-6 for a,b in zip(binding['target_vector'],vectors[0]))):
        raise DomainError('CALIBRATION_VERSION_MISMATCH','Frozen calibration model or target changed',409)
    raw=[sum(a*b for a,b in zip(vectors[0],v)) for v in vectors[1:]]
    samples=[(value,pair['human_rating']) for value,pair in zip(raw,body['pairs']) if pair['human_rating']!='exclude']
    calibration=fit_calibration(samples) if body['split']=='calibration' else puzzle.get('calibration')
    if not calibration: raise DomainError('CALIBRATION_REQUIRED','A completed calibration split is required',409)
    rows=[]
    for pair,value in zip(body['pairs'],raw):
        scaled=99.9*min(1,max(0,(value-calibration['b'])/(calibration['u']-calibration['b'])))**calibration['gamma']
        rows.append(pair|{'raw_score':value,'display_score':round(min(99.9,scaled),1)})
    ordered=[(a,b) for a in range(len(samples)) for b in range(a+1,len(samples)) if samples[a][1]!=samples[b][1]]
    order={'cold':0,'warm':1,'hot':2}
    concordant=sum((samples[a][0]-samples[b][0])*(order[samples[a][1]]-order[samples[b][1]])>0 for a,b in ordered)
    return {'calibration':calibration,'target_vector':vectors[0],'dimension':dimension,'normalization':'unit-l2/1','manifest_id':body['manifest_id'],'manifest_sha256':getattr(scorer,'manifest_sha256',None),'pairs':rows,'metrics':{'pairwise_accuracy':concordant/len(ordered) if ordered else None,'group_means':{label:sum(v for v,r in samples if r==label)/sum(r==label for v,r in samples) for label in order if any(r==label for v,r in samples)},'sample_count':len(samples)},'quality_scope':'single-puzzle-evaluation','global_G1_Q':False}
