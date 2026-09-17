"""Opt-in integration rehearsal. Creates/removes only unique synthetic QA schemas.

Run only with authorization: python test_live_migration.py --run-isolated
Reads the active local profile without printing credentials.
"""
import argparse
import os
from dataclasses import fields
from pathlib import Path
import json
import tempfile
import uuid

from pgbridge.engine import (Connections, PgConfig, MsConfig, Introspector,
                             Options, Transport, Verifier, Preflight)


def run():
    raw=json.loads((Path.home()/'.pgbridge/profiles.json').read_text())
    profile=raw.get('profiles',{}).get(raw.get('active_profile','Default'),raw)
    def cfg(cls,key):
        return cls(**{k:v for k,v in profile.get(key,{}).items() if k in {f.name for f in fields(cls)}})
    pg,ms=cfg(PgConfig,'pg'),cfg(MsConfig,'ms')
    pg.dbname=ms.database=os.environ['PGBRIDGE_QA_DB']
    schema='pgbridge_qa_'+uuid.uuid4().hex[:12]
    pg.schema=ms.schema=schema
    conns=Connections(pg,ms)
    source=target=None
    source_created=target_created=False
    try:
        source=conns.source()
        target=conns.target()
        target.autocommit=True
        source.cursor().execute(f'CREATE SCHEMA "{schema}"')
        source_created=True
        target.cursor().execute(f'CREATE SCHEMA [{schema}]')
        target_created=True
        cur=source.cursor()
        cur.execute(f'''CREATE TABLE "{schema}".records (
            id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            label text NOT NULL, token uuid NOT NULL,
            amount numeric(18,4), active boolean, payload jsonb,
            moment timestamp with time zone, binary_data bytea,
            unlimited character varying
        )''')
        cur.execute(f'CREATE TABLE "{schema}".empty_records (id integer PRIMARY KEY, label text)')
        for index in range(3):
            cur.execute(f'''INSERT INTO "{schema}".records
                (label,token,amount,active,payload,moment,binary_data,unlimited)
                VALUES (%s,%s::uuid,%s,%s,%s::jsonb,%s::timestamptz,%s,%s)''',
                ('مرحبا · QA '+str(index),str(uuid.uuid4()),'123.4567' if index else None,
                 bool(index),json.dumps({'count':index,'name':'QA'}),
                 '2026-09-15 12:34:56.123456+05:00',b'\x00\x01\xff', 'x'*5000))
        tables=Introspector(conns).discover()
        with tempfile.TemporaryDirectory() as folder:
            options=Options(output_dir=folder,chunk_size=2)
            issues=Preflight(conns,tables,options).run()
            blocking=[i for i in issues if i.level=='stop']
            if blocking:raise AssertionError(str([(i.check,i.detail) for i in blocking]))
            transport=Transport(conns,tables,options)
            transport._run()
            outcome=[e for e in list(transport.events.queue) if e['kind']=='finished'][-1]
            print(json.dumps({'phase':'transfer','outcome':outcome,'results':transport.results},default=str),flush=True)
            assert outcome['tables_ok']==2 and outcome['rows']==3 and not outcome.get('error'), 'Synthetic transfer failed'
            for level in ('counts','profile','full'):
                report=Verifier(conns,tables,options).run(level=level)
                print(json.dumps({'phase':'verify','level':level,'passed':report['passed'],
                                  'tables':report['tables'],'identity_problems':report['identity_problems']},default=str),flush=True)
                assert report['passed'],f'{level} verification failed'
            # Exercise the existing-schema path with concurrent table workers.
            refreshed=Introspector(conns).discover()
            data_options=Options(output_dir=folder,chunk_size=2,mode='data',workers=2)
            for resume in (False,True):
                data_options.resume=resume
                repeated=Transport(conns,refreshed,data_options)
                repeated._run()
                result=[e for e in list(repeated.events.queue) if e['kind']=='finished'][-1]
                assert result['tables_ok']==2 and result['rows']==3 and not result.get('error'), result
                print(json.dumps({'phase':'resume' if resume else 'parallel_data_only','passed':True}),flush=True)
            assert Verifier(conns,refreshed,data_options).run(level='full')['passed']
            cur=target.cursor()
            cur.execute(f"INSERT INTO [{schema}].records (label,token) VALUES (?,?)",('new key',uuid.uuid4().hex))
            cur.execute(f'SELECT MAX(id) FROM [{schema}].records')
            assert cur.fetchone()[0]==4, 'Identity sequence did not continue after migration'
            print(json.dumps({'phase':'identity_insert','passed':True}),flush=True)
    finally:
        if target:
            try:
                if target_created:
                    cur=target.cursor()
                    for table in ('records','empty_records'):
                        cur.execute(f"IF OBJECT_ID(N'[{schema}].[{table}]', N'U') IS NOT NULL DROP TABLE [{schema}].[{table}]")
                    cur.execute(f'DROP SCHEMA [{schema}]')
            finally:target.close()
        if source:
            try:
                if source_created:source.cursor().execute(f'DROP SCHEMA "{schema}" CASCADE')
            finally:source.close()
        print(json.dumps({'phase':'cleanup','schema':schema,'complete':True}),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-isolated',action='store_true',required=True)
    parser.parse_args()
    run()
