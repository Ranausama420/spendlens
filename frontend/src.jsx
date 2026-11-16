import React, {useEffect, useState} from 'react';
import {createRoot} from 'react-dom/client';
import './style.css';

const money = value => new Intl.NumberFormat('en-US', {
  style:'currency', currency:'USD', maximumFractionDigits:Math.abs(value)<10_000_000 ? 4 : 2,
}).format(value / 1e9);
const dateLabel = day => new Date(day + 'T00:00:00Z').toLocaleDateString('en-US',{month:'short',day:'numeric',timeZone:'UTC'});
const dimensions = {tenant:'Tenant',feature:'Feature',model:'Model'};

function Chart({timeline, day, chooseDay}) {
  const peak = Math.max(...timeline.map(point=>point.cost_nanos),1);
  return <>
    <div className="chart" aria-label="Daily spend in the last 30 days">
      {timeline.map(point=><button type="button" key={point.day}
        title={dateLabel(point.day)+' · '+money(point.cost_nanos)}
        aria-label={dateLabel(point.day)+' spend '+money(point.cost_nanos)}
        aria-pressed={day===point.day}
        className={point.cost_nanos===peak?'peak':''}
        onClick={()=>chooseDay(day===point.day?'':point.day)}>
        <span style={{height:Math.max(2,point.cost_nanos/peak*100)+'%'}}/>
      </button>)}
    </div>
    <div className="chart-labels"><span>{dateLabel(timeline[0].day)}</span><span>{dateLabel(timeline.at(-1).day)}</span></div>
  </>;
}

function App(){
  const [filters,setFilters]=useState({tenant:'',feature:'',model:''});
  const [day,setDay]=useState('');
  const [page,setPage]=useState(1);
  const [data,setData]=useState(null);
  const [error,setError]=useState('');
  const [token,setToken]=useState('');
  const [draft,setDraft]=useState('');
  const [locked,setLocked]=useState(false);
  const [dimension,setDimension]=useState('tenant');

  useEffect(()=>{
    const controller=new AbortController();
    const query=new URLSearchParams({page});
    Object.entries({...filters,day}).forEach(([key,value])=>{if(value) query.set(key,value);});
    setError('');
    fetch('/api/dashboard?'+query,{signal:controller.signal,headers:token?{Authorization:'Bearer '+token}:{}})
      .then(async response=>{
        if (response.status===401){setLocked(true); setData(null); return;}
        if (!response.ok) throw new Error('Could not load usage. Refresh and try again.');
        const body=await response.json();
        if(controller.signal.aborted) return;
        setData(body);setLocked(false);
      }).catch(error=>{if(error.name!=='AbortError')setError(error.message);});
    return ()=>controller.abort();
  },[filters,day,page,token]);

  function filter(key,value){
    setFilters(current=>({...current,[key]:value}));
    setDay('');setPage(1);
  }
  function selectDay(value){setDay(value);setPage(1);}
  const peak=data?.timeline.reduce((best,point)=>point.cost_nanos>best.cost_nanos?point:best);
  return <>
    <header><div className="logo"><span className="mark" aria-hidden="true">▥</span>SpendLens</div><span className="header-note">USD · UTC</span></header>
    <main>
      <div className="intro"><div><h1>Usage & spend</h1><p>Trace model costs to the requests that drove them.</p></div><span className="period">Last 30 days</span></div>
      {error && <p className="panel" role="alert">{error}</p>}
      {locked && <form className="panel" onSubmit={event=>{event.preventDefault();setToken(draft.trim());setDraft('');}}>
        <label>Workspace token <input type="password" value={draft} required onChange={event=>setDraft(event.target.value)}/></label>
        <button className="accent">Unlock dashboard</button>
      </form>}
      {!data&&!locked&&!error&&<p role="status">Loading dashboard…</p>}
      {data&&<>
        <div className="filters">{Object.entries(dimensions).map(([key,label])=><label key={key}>{label}
          <select value={filters[key]} onChange={event=>filter(key,event.target.value)}>
            <option value="">All {label.toLowerCase()}s</option>
            {data.options[key].map(value=><option key={value}>{value}</option>)}
          </select></label>)}</div>
        <div className="grid">
          <section className="panel trend">
            <div className="panel-head"><div><h2>Total spend</h2><strong className="amount">{money(data.total_nanos)}</strong></div>
              {peak?.cost_nanos>0 && <button className="peak-link" onClick={()=>selectDay(peak.day)}>Peak {dateLabel(peak.day)} · {money(peak.cost_nanos)} ↘</button>}</div>
            <Chart timeline={data.timeline} day={day} chooseDay={selectDay}/>
          </section>
          <section className="panel breakdown">
            <div className="panel-head"><h2>Spend by</h2><select aria-label="Breakdown dimension" value={dimension} onChange={e=>setDimension(e.target.value)}>
              {Object.entries(dimensions).map(([key,label])=><option key={key} value={key}>{label}</option>)}
            </select></div>
            {data.breakdown[dimension].map(row=><button className="breakdown-row" key={row.key} onClick={()=>filter(dimension,row.key)}>
              <span>{row.key}</span><strong>{money(row.cost_nanos)}</strong>
              <span className="meter"><i style={{width:(data.total_nanos?row.cost_nanos/data.total_nanos*100:0)+'%'}}/></span>
            </button>)}
            {!data.breakdown[dimension].length&&<p>No spend for these filters.</p>}
          </section>
        </div>
        <section className="panel budgets"><h2>Monthly budgets</h2>
          {data.budgets.map(budget=><div className="budget" key={budget.tenant}>
            <div className="panel-head"><button onClick={()=>filter('tenant',budget.tenant)}>{budget.tenant}</button>
              <strong>{money(budget.spend_nanos)} / {money(budget.limit_nanos)}</strong></div>
            <div className="meter"><i style={{width:Math.min(100,budget.spend_nanos/budget.limit_nanos*100)+'%'}}/></div>
            {data.alerts.filter(alert=>alert.tenant===budget.tenant).map(alert=><span className="badge" key={alert.threshold}>
              {alert.threshold}% reached at {money(alert.spend_nanos)}
            </span>)}
          </div>)}
          {!data.budgets.length&&<p>No budget configured for this month.</p>}
        </section>
        <section className="panel requests" aria-label="Usage requests">
          <div className="panel-head"><h2>Requests {day&&<span>· {dateLabel(day)}</span>}</h2>{day&&<button onClick={()=>selectDay('')}>Show all days</button>}</div>
          <div className="table-wrap"><table><thead><tr>
            <th>Request ID</th><th>Tenant</th><th>Feature</th><th>Model</th><th>Input / output</th><th>Cost</th>
          </tr></thead><tbody>{data.events.map(row=><tr key={row.tenant+'/'+row.request_id}>
            <th scope="row">{row.request_id}</th><td>{row.tenant}</td><td>{row.feature}</td><td>{row.model}</td>
            <td>{row.input_tokens.toLocaleString()} / {row.output_tokens.toLocaleString()}</td><td>{money(row.cost_nanos)}</td>
          </tr>)}</tbody></table></div>
          {!data.events.length&&<p>No matching requests.</p>}
          <div className="pagination"><span>Page {page}</span><button disabled={page===1} onClick={()=>setPage(page-1)}>Previous</button>
            <button disabled={!data.has_next} onClick={()=>setPage(page+1)}>Next</button></div>
        </section>
      </>}
    </main>
  </>;
}

createRoot(document.getElementById('root')).render(<App/>);
