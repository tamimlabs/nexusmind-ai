
const chips=['All','Featured','New','Trending','Collections','Live'];
chips.forEach((c,i)=>{const d=document.createElement('div');d.className='chip'+(i?'':' active');
d.textContent=c;d.onclick=()=>document.querySelectorAll('.chip').forEach(x=>x.classList.remove('active'))||d.classList.add('active');
document.getElementById('chips').appendChild(d)});
const titles=['Concept one — hero section','Concept two — immersive grid',
'Concept three — ambient mode','Concept four — focus feed','Concept five — gesture nav'];
for(let i=0;i<12;i++){const t=titles[i%titles.length];
document.getElementById('grid').insertAdjacentHTML('beforeend',
`<div><div class=thumb><span class=badge>${(i+3)*2}:${i%6}0</span></div>
<div class=meta><div class=avatar></div><div><div class=t>${i+1}. ${t}</div>
<div class=s>NexusMind Labs &#8226; ${(i+1)*11}K views</div></div></div></div>`)};
