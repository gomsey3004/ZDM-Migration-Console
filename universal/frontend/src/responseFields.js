const assignment=/^([ \t]*)([A-Z][A-Z0-9_.-]*)([ \t]*=[ \t]*)(.*?)(\r?)$/;
export function parameters(content){return content.split('\n').flatMap((line,index)=>{const m=line.match(assignment);return m?[{key:m[2],value:m[4],index}]:[]})}
export function updateParameter(content,index,key,value){
 if(/[\r\n\0]/.test(value))throw new Error('Use a single-line parameter value.');
 if(index===null)return addParameter(content,key,value);
 const lines=content.split('\n'),m=lines[index]?.match(assignment);
 if(!m||m[2]!==key)throw new Error('The parameter changed. Reload the field before editing.');
 lines[index]=m[1]+m[2]+m[3]+value+m[5];return lines.join('\n');
}
export function addParameter(content,key,value=''){
 if(!/^[A-Z][A-Z0-9_.-]{0,127}$/.test(key))throw new Error('Enter an uppercase parameter name.');
 if(parameters(content).some(row=>row.key===key))throw new Error('This parameter already exists. Edit its current field.');
 if(/[\r\n\0]/.test(value))throw new Error('Use a single-line parameter value.');
 const eol=content.includes('\r\n')?'\r\n':'\n';return content+(content&&!content.endsWith('\n')?eol:'')+key+'='+value+eol;
}
export function removeParameter(content,index,key){const lines=content.split('\n');if(lines[index]?.match(assignment)?.[2]!==key)throw new Error('The parameter changed.');lines.splice(index,1);return lines.join('\n')}
