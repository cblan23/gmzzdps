"""Bounded entity creation decoding from passive application envelopes.

Property indices come from the locally inspected c7-2026-09-02 net definitions,
cross-checked with the recorded B/C/D scene spawns. Unknown classes/fields are
not guessed. These are property tables, never RPC method-id tables.
"""
import msgpack

PROPERTIES = {
    'AvatarActor':{0:'Name',3:'Level',5:'Profession',83:'FinalOwnerUID',92:'LogicServerID'},
    'NpcActor':{0:'TemplateID',1:'Level',3:'BossType',66:'FinalOwnerUID',75:'LogicServerID'},
    'Space':{4:'WorldID',5:'WorldEntityID',8:'TemplateID',11:'ApplyTemplateID',30:'LogicServerID'},
    'MainCitySpace':{4:'WorldID',5:'WorldEntityID',8:'TemplateID',11:'ApplyTemplateID',30:'LogicServerID'},
    'DungeonSpace':{16:'WorldID',17:'WorldEntityID',20:'TemplateID',23:'ApplyTemplateID',42:'LogicServerID'},
    # Verified against this client's PVPLastHuntSpace property descriptors
    # on 2026-09-18. Its thirteen leading gameplay fields shift the inherited
    # Space fields; treating it as ordinary Space loses the map boundary.
    'PVPLastHuntSpace':{17:'WorldID',18:'WorldEntityID',21:'TemplateID',24:'ApplyTemplateID',44:'LogicServerID'},
}

SPACE_CLASSES = frozenset({'Space', 'MainCitySpace', 'DungeonSpace', 'PVPLastHuntSpace'})


def _text(value):
    if isinstance(value, bytes):
        value=value.decode('utf-8')
    if not isinstance(value,str) or len(value)>256:
        raise ValueError('Invalid entity text')
    return value


def decode_creation(kind, value):
    if kind not in (7,11) or not isinstance(value,list) or len(value)!=2:
        return None
    call=value[1]
    if not isinstance(call,list) or len(call)!=4:
        return None
    if kind==7:cls,token,entity,packed=call
    else:token,entity,cls,packed=call
    cls,token=_text(cls),_text(token)
    if isinstance(entity,bool) or not isinstance(entity,int) or not 0<entity<2**64:
        raise ValueError('Invalid entity id')
    if not isinstance(packed,bytes) or len(packed)>4*1024*1024:
        raise ValueError('Invalid entity property blob')
    properties=msgpack.unpackb(packed,raw=True,strict_map_key=False)
    if not isinstance(properties,list) or not properties or not isinstance(properties[0],dict):
        raise ValueError('Invalid entity creation properties')
    fields={}
    for index,name in PROPERTIES.get(cls,{}).items():
        if index not in properties[0]:
            continue
        field=properties[0][index]
        if name in ('Name','WorldEntityID'):
            fields[name]=_text(field)
        elif isinstance(field,bool) or not isinstance(field,int) or not 0<=field<2**64:
            raise ValueError('Invalid entity numeric property')
        else:fields[name]=field
    return {'entity_id':entity,'entity_token':token,'entity_class':cls,'properties':fields,
            'schema':'c7-2026-09-02-properties','known_property_schema':cls in PROPERTIES}


def validate_wrapper(data, size):
    """Validate a forwarding header without waiting for its streamed children."""
    header=6+data[5]
    if header>size:
        raise ValueError('Invalid forwarding metadata length')
    if len(data)<header:
        raise EOFError('Incomplete forwarding metadata')
    metadata=msgpack.unpackb(bytes(data[6:header]),raw=True,strict_map_key=False)
    if not isinstance(metadata,dict):
        raise ValueError('Invalid forwarding metadata')
    return header
