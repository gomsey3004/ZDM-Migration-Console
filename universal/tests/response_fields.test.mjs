import test from 'node:test';
import assert from 'node:assert/strict';
import {parameters,updateParameter,addParameter,removeParameter} from '../frontend/src/responseFields.js';
test('field edit preserves comments, custom fields, spacing and CRLF',()=>{const s='# comment\r\n  MIGRATION_METHOD = ONLINE_PHYSICAL\r\nCUSTOM.X=abc=123\r\n';const r=parameters(s);assert.equal(r.length,2);assert.equal(updateParameter(s,r[0].index,r[0].key,'OFFLINE_PHYSICAL'),s.replace('ONLINE_PHYSICAL','OFFLINE_PHYSICAL'));assert.equal(parameters(s)[1].value,'abc=123')});
test('duplicate rows update individually and reject duplicate additions',()=>{const s='A=first\nA=second\n';assert.equal(updateParameter(s,1,'A','third'),'A=first\nA=third\n');assert.throws(()=>addParameter(s,'A','x'));assert.throws(()=>updateParameter(s,1,'B','x'))});
test('add remove and single-line guards',()=>{assert.equal(addParameter('# comment','A','v'),'# comment\nA=v\n');assert.equal(removeParameter('# comment\nA=v\n',1,'A'),'# comment\n');assert.throws(()=>addParameter('','A','x\nB=y'));assert.throws(()=>updateParameter('A=x',0,'A','\0'))});
