#!/usr/bin/env python3
"""
Jira Dashboard Generator for WMI Solutions
Fetches epics from Jira Cloud, classifies them as Novo/Upsell, and generates an HTML dashboard.
"""

import os
import sys
import json
import base64
from datetime import datetime
from typing import Dict, List, Tuple, Optional
import requests
from collections import defaultdict

# Configuration
JIRA_EMAIL = os.getenv('JIRA_EMAIL')
JIRA_API_TOKEN = os.getenv('JIRA_API_TOKEN')
JIRA_BASE_URL = os.getenv('JIRA_BASE_URL')

IMPLEMENTERS = ['Anderson', 'Luiz', 'Jorge', 'Nino', 'Fernanda', 'Jessica', 'Daniel', 'Fabio']
EXCLUDE_ASSIGNEES = ['Yasmin', 'Michael', 'Iris']

# Status mappings
STATUS_COMPLETED = {'Concluído', 'Cancelado'}
STATUS_EM_ANDAMENTO = {'Em andamento'}
STATUS_PAUSED = {'Paused'}
STATUS_PENDENTE = {'Tarefas pendentes', 'Aguardando cliente', 'Escalado'}

class JiraClient:
    """Client for Jira Cloud REST API"""

    def __init__(self, email: str, api_token: str, base_url: str):
        if not all([email, api_token, base_url]):
            raise ValueError("JIRA_EMAIL, JIRA_API_TOKEN, and JIRA_BASE_URL environment variables are required")

        self.base_url = base_url.rstrip('/')
        self.email = email
        auth_string = f"{email}:{api_token}"
        encoded_auth = base64.b64encode(auth_string.encode()).decode()
        self.headers = {
            'Authorization': f'Basic {encoded_auth}',
            'Accept': 'application/json',
            'Content-Type': 'application/json'
        }

    def get_epics(self) -> List[Dict]:
        """Fetch all epics from project IWN with pagination"""
        epics = []
        start_at = 0
        max_results = 100

        jql = 'project = IWN AND issuetype = Epic'
        fields = [
            'summary',
            'status',
            'assignee',
            'customfield_10800',
            'aggregatetimespent',
            'created',
            'updated'
        ]

        while True:
            try:
                url = f"{self.base_url}/rest/api/3/search/jql"
                payload = {
                    'jql': jql,
                    'startAt': start_at,
                    'maxResults': max_results,
                    'fields': fields
                }

                response = requests.post(url, headers=self.headers, json=payload, timeout=30)
                response.raise_for_status()

                data = response.json()
                issues = data.get('issues', [])

                if not issues:
                    break

                epics.extend(issues)

                # Check if there are more results
                if len(issues) < max_results:
                    break

                start_at += max_results

            except requests.exceptions.RequestException as e:
                print(f"Error fetching epics: {e}", file=sys.stderr)
                raise

        return epics

def classify_epic(epic: Dict) -> str:
    """Classify epic as Novo or Upsell based on customfield_10800"""
    fields = epic.get('fields', {})
    tipo_negocio = fields.get('customfield_10800')

    if tipo_negocio is None:
        return 'Upsell'  # Default to Upsell if empty

    tipo_str = str(tipo_negocio).lower()

    if 'empresa nova' in tipo_str:
        return 'Novo'
    elif 'empresa existente' in tipo_str:
        return 'Upsell'
    else:
        return 'Upsell'  # Default to Upsell

def get_status_category(status: str) -> str:
    """Map Jira status to internal status categories"""
    if status in STATUS_COMPLETED:
        return 'completed'
    elif status in STATUS_EM_ANDAMENTO:
        return 'em_andamento'
    elif status in STATUS_PAUSED:
        return 'paused'
    elif status in STATUS_PENDENTE:
        return 'pendente'
    else:
        return 'pendente'  # Default

def extract_implementer_name(assignee: Optional[Dict]) -> Optional[str]:
    """Extract implementer name from assignee object"""
    if not assignee:
        return None

    display_name = assignee.get('displayName', '')
    # Extract first name or use display name
    for impl in IMPLEMENTERS:
        if impl.lower() in display_name.lower():
            return impl

    return None

def process_epics(epics: List[Dict]) -> Dict:
    """Process epics and calculate metrics"""
    metrics = {
        'implementers': {},
        'total': {
            'count': 0,
            'novo': {'total': 0, 'completed': 0, 'em_andamento': 0, 'paused': 0, 'pendente': 0},
            'upsell': {'total': 0, 'completed': 0, 'em_andamento': 0, 'paused': 0, 'pendente': 0}
        },
        'epics_list': []
    }

    # Initialize implementer metrics
    for impl in IMPLEMENTERS:
        metrics['implementers'][impl] = {
            'novo': {'total': 0, 'completed': 0, 'em_andamento': 0, 'paused': 0, 'pendente': 0},
            'upsell': {'total': 0, 'completed': 0, 'em_andamento': 0, 'paused': 0, 'pendente': 0},
            'total_hours': 0,
            'epics': []
        }

    for epic in epics:
        fields = epic.get('fields', {})
        summary = fields.get('summary', 'Unknown')
        status = fields.get('status', {}).get('name', 'Unknown')
        status_category = get_status_category(status)
        assignee = fields.get('assignee')
        classificacao = classify_epic(epic)
        time_spent = fields.get('aggregatetimespent', 0) or 0
        hours = time_spent / 3600 if time_spent else 0

        implementer = extract_implementer_name(assignee)

        if not implementer:
            continue

        # Update total metrics
        metrics['total']['count'] += 1
        metrics['total'][classificacao]['total'] += 1
        metrics['total'][classificacao][status_category] += 1

        # Update implementer metrics
        metrics['implementers'][implementer][classificacao]['total'] += 1
        metrics['implementers'][implementer][classificacao][status_category] += 1
        metrics['implementers'][implementer]['total_hours'] += hours

        epic_data = {
            'key': epic.get('key', ''),
            'summary': summary,
            'status': status,
            'implementer': implementer,
            'classificacao': classificacao,
            'hours': round(hours, 2),
            'created': fields.get('created', ''),
            'updated': fields.get('updated', '')
        }
        metrics['implementers'][implementer]['epics'].append(epic_data)
        metrics['epics_list'].append(epic_data)

    return metrics

def calculate_completion_rate(status_dict: Dict) -> float:
    """Calculate completion rate percentage"""
    total = status_dict.get('total', 0)
    if total == 0:
        return 0
    completed = status_dict.get('completed', 0)
    return round((completed / total) * 100, 1)

def generate_html_dashboard(metrics: Dict) -> str:
    """Generate complete HTML dashboard"""

    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    # Calculate KPIs
    novo_count = metrics['total']['novo']['total']
    upsell_count = metrics['total']['upsell']['total']
    total_count = metrics['total']['count']
    novo_pct = round((novo_count / total_count * 100), 1) if total_count > 0 else 0
    upsell_pct = round((upsell_count / total_count * 100), 1) if total_count > 0 else 0
    novo_completion = calculate_completion_rate(metrics['total']['novo'])
    upsell_completion = calculate_completion_rate(metrics['total']['upsell'])

    # Prepare data for charts
    implementer_names = list(IMPLEMENTERS)
    novo_counts = [metrics['implementers'][impl]['novo']['total'] for impl in implementer_names]
    upsell_counts = [metrics['implementers'][impl]['upsell']['total'] for impl in implementer_names]
    total_hours_list = [round(metrics['implementers'][impl]['total_hours'], 1) for impl in implementer_names]

    # Completion breakdown by implementer for Novo
    novo_completed = [metrics['implementers'][impl]['novo']['completed'] for impl in implementer_names]
    novo_em_andamento = [metrics['implementers'][impl]['novo']['em_andamento'] for impl in implementer_names]
    novo_paused = [metrics['implementers'][impl]['novo']['paused'] for impl in implementer_names]
    novo_pendente = [metrics['implementers'][impl]['novo']['pendente'] for impl in implementer_names]

    # Completion breakdown by implementer for Upsell
    upsell_completed = [metrics['implementers'][impl]['upsell']['completed'] for impl in implementer_names]
    upsell_em_andamento = [metrics['implementers'][impl]['upsell']['em_andamento'] for impl in implementer_names]
    upsell_paused = [metrics['implementers'][impl]['upsell']['paused'] for impl in implementer_names]
    upsell_pendente = [metrics['implementers'][impl]['upsell']['pendente'] for impl in implementer_names]

    # Average hours per epic
    novo_avg_hours = round((sum([m['novo']['total'] for m in metrics['implementers'].values()]) > 0 and
                            sum([sum([e.get('hours', 0) for e in m['epics'] if e['classificacao'] == 'Novo'])
                                for m in metrics['implementers'].values()]) /
                            sum([m['novo']['total'] for m in metrics['implementers'].values()]) or 0), 1)

    upsell_avg_hours = round((sum([m['upsell']['total'] for m in metrics['implementers'].values()]) > 0 and
                              sum([sum([e.get('hours', 0) for e in m['epics'] if e['classificacao'] == 'Upsell'])
                                  for m in metrics['implementers'].values()]) /
                              sum([m['upsell']['total'] for m in metrics['implementers'].values()]) or 0), 1)

    # Behavior analysis
    novo_total_hours = sum([m['novo']['total'] for m in metrics['implementers'].values()])
    upsell_total_hours = sum([m['upsell']['total'] for m in metrics['implementers'].values()])
    novo_em_and_pct = round((sum([m['novo']['em_andamento'] for m in metrics['implementers'].values()]) / novo_total_hours * 100), 1) if novo_total_hours > 0 else 0
    upsell_em_and_pct = round((sum([m['upsell']['em_andamento'] for m in metrics['implementers'].values()]) / upsell_total_hours * 100), 1) if upsell_total_hours > 0 else 0

    # Risk analysis
    at_risk_implementers = []
    for impl in implementer_names:
        total_epics = metrics['implementers'][impl]['novo']['total'] + metrics['implementers'][impl]['upsell']['total']
        if total_epics > 0:
            completion_rate = ((metrics['implementers'][impl]['novo']['completed'] +
                              metrics['implementers'][impl]['upsell']['completed']) / total_epics) * 100
            if completion_rate < 50:
                at_risk_implementers.append({
                    'name': impl,
                    'completion': round(completion_rate, 1),
                    'pending': metrics['implementers'][impl]['novo']['pendente'] + metrics['implementers'][impl]['upsell']['pendente']
                })

    html = f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Dashboard: Novo Cliente vs Upsell/Existente | WMI Solutions</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4.5.0/dist/chart.umd.js" integrity="sha384-iU8HYtnGQ8Cy4zl7gbNMOhsDTTKX02BTXptVP/vqAWIaTfM7isw76iyZCsjL2eVi" crossorigin="anonymous"></script>
    <style>
        :root {{
            --bg-primary: #0d1117;
            --bg-secondary: #161b22;
            --bg-tertiary: #21262d;
            --text-primary: #c9d1d9;
            --text-secondary: #8b949e;
            --border-color: #30363d;
            --novo: #06b6d4;
            --upsell: #f97316;
            --success: #22c55e;
            --warning: #f59e0b;
            --danger: #ef4444;
            --info: #3b82f6;
        }}

        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }}

        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'Roboto', 'Oxygen', 'Ubuntu', 'Cantarell', sans-serif;
            background-color: var(--bg-primary);
            color: var(--text-primary);
            line-height: 1.6;
        }}

        .container {{
            max-width: 1600px;
            margin: 0 auto;
            padding: 20px;
        }}

        header {{
            text-align: center;
            margin-bottom: 40px;
            padding-bottom: 20px;
            border-bottom: 2px solid var(--border-color);
        }}

        header h1 {{
            font-size: 2.5em;
            margin-bottom: 10px;
            background: linear-gradient(135deg, var(--novo), var(--upsell));
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
        }}

        header p {{
            color: var(--text-secondary);
            font-size: 0.95em;
        }}

        .tabs {{
            display: flex;
            gap: 10px;
            margin-bottom: 30px;
            flex-wrap: wrap;
            border-bottom: 2px solid var(--border-color);
        }}

        .tab-button {{
            padding: 12px 20px;
            background-color: transparent;
            color: var(--text-secondary);
            border: none;
            border-bottom: 3px solid transparent;
            cursor: pointer;
            font-size: 0.95em;
            font-weight: 500;
            transition: all 0.3s ease;
        }}

        .tab-button:hover {{
            color: var(--text-primary);
            border-bottom-color: var(--info);
        }}

        .tab-button.active {{
            color: var(--text-primary);
            border-bottom-color: var(--novo);
        }}

        .tab-content {{
            display: none;
        }}

        .tab-content.active {{
            display: block;
            animation: fadeIn 0.3s ease;
        }}

        @keyframes fadeIn {{
            from {{ opacity: 0; transform: translateY(10px); }}
            to {{ opacity: 1; transform: translateY(0); }}
        }}

        .kpi-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
            gap: 20px;
            margin-bottom: 30px;
        }}

        .kpi-card {{
            background-color: var(--bg-secondary);
            border: 1px solid var(--border-color);
            border-radius: 8px;
            padding: 20px;
            transition: all 0.3s ease;
        }}

        .kpi-card:hover {{
            border-color: var(--text-secondary);
            transform: translateY(-2px);
        }}

        .kpi-card.novo {{
            border-left: 4px solid var(--novo);
        }}

        .kpi-card.upsell {{
            border-left: 4px solid var(--upsell);
        }}

        .kpi-card.success {{
            border-left: 4px solid var(--success);
        }}

        .kpi-label {{
            color: var(--text-secondary);
            font-size: 0.85em;
            margin-bottom: 8px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }}

        .kpi-value {{
            font-size: 2em;
            font-weight: 700;
            margin-bottom: 5px;
        }}

        .kpi-card.novo .kpi-value {{
            color: var(--novo);
        }}

        .kpi-card.upsell .kpi-value {{
            color: var(--upsell);
        }}

        .kpi-card.success .kpi-value {{
            color: var(--success);
        }}

        .kpi-subtitle {{
            color: var(--text-secondary);
            font-size: 0.85em;
        }}

        .chart-container {{
            background-color: var(--bg-secondary);
            border: 1px solid var(--border-color);
            border-radius: 8px;
            padding: 20px;
            margin-bottom: 20px;
            position: relative;
            height: 400px;
        }}

        .chart-container.wide {{
            grid-column: 1 / -1;
        }}

        .chart-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(500px, 1fr));
            gap: 20px;
            margin-bottom: 30px;
        }}

        .chart-container.full {{
            grid-column: 1 / -1;
            height: 450px;
        }}

        .chart-title {{
            font-size: 1.2em;
            font-weight: 600;
            margin-bottom: 15px;
            color: var(--text-primary);
        }}

        .table-container {{
            background-color: var(--bg-secondary);
            border: 1px solid var(--border-color);
            border-radius: 8px;
            padding: 20px;
            margin-bottom: 20px;
            overflow-x: auto;
        }}

        table {{
            width: 100%;
            border-collapse: collapse;
        }}

        th {{
            background-color: var(--bg-tertiary);
            color: var(--text-primary);
            padding: 12px;
            text-align: left;
            font-weight: 600;
            border-bottom: 2px solid var(--border-color);
        }}

        td {{
            padding: 12px;
            border-bottom: 1px solid var(--border-color);
            color: var(--text-primary);
        }}

        tr:hover {{
            background-color: var(--bg-tertiary);
        }}

        .status-badge {{
            display: inline-block;
            padding: 4px 8px;
            border-radius: 4px;
            font-size: 0.85em;
            font-weight: 500;
        }}

        .status-completed {{
            background-color: rgba(34, 197, 94, 0.2);
            color: var(--success);
        }}

        .status-em-andamento {{
            background-color: rgba(59, 130, 246, 0.2);
            color: var(--info);
        }}

        .status-paused {{
            background-color: rgba(245, 158, 11, 0.2);
            color: var(--warning);
        }}

        .status-pendente {{
            background-color: rgba(239, 68, 68, 0.2);
            color: var(--danger);
        }}

        .badge {{
            display: inline-block;
            padding: 4px 10px;
            border-radius: 4px;
            font-size: 0.8em;
            font-weight: 500;
            margin-right: 5px;
        }}

        .badge.novo {{
            background-color: rgba(6, 182, 212, 0.2);
            color: var(--novo);
        }}

        .badge.upsell {{
            background-color: rgba(249, 115, 22, 0.2);
            color: var(--upsell);
        }}

        .risk-high {{
            background-color: rgba(239, 68, 68, 0.1);
            border-left: 4px solid var(--danger);
        }}

        .risk-medium {{
            background-color: rgba(245, 158, 11, 0.1);
            border-left: 4px solid var(--warning);
        }}

        .risk-low {{
            background-color: rgba(34, 197, 94, 0.1);
            border-left: 4px solid var(--success);
        }}

        footer {{
            text-align: center;
            padding-top: 30px;
            border-top: 2px solid var(--border-color);
            margin-top: 50px;
            color: var(--text-secondary);
            font-size: 0.9em;
        }}

        .metric-row {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 15px;
            margin-bottom: 20px;
        }}

        .metric-item {{
            padding: 10px;
            background-color: var(--bg-tertiary);
            border-radius: 4px;
            border-left: 3px solid var(--info);
        }}

        .metric-item.novo {{
            border-left-color: var(--novo);
        }}

        .metric-item.upsell {{
            border-left-color: var(--upsell);
        }}

        .metric-label {{
            color: var(--text-secondary);
            font-size: 0.85em;
            margin-bottom: 4px;
        }}

        .metric-value {{
            color: var(--text-primary);
            font-size: 1.5em;
            font-weight: 600;
        }}

        .recommendation-box {{
            background-color: var(--bg-secondary);
            border-left: 4px solid var(--success);
            padding: 15px;
            border-radius: 4px;
            margin-bottom: 15px;
        }}

        .recommendation-box h4 {{
            color: var(--success);
            margin-bottom: 8px;
        }}

        .recommendation-box p {{
            color: var(--text-secondary);
            font-size: 0.95em;
        }}

        @media (max-width: 768px) {{
            .kpi-grid {{
                grid-template-columns: 1fr;
            }}

            .chart-grid {{
                grid-template-columns: 1fr;
            }}

            .chart-container {{
                height: 300px;
            }}

            header h1 {{
                font-size: 1.8em;
            }}

            .tabs {{
                flex-direction: column;
            }}

            .tab-button {{
                width: 100%;
                text-align: left;
            }}
        }}
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1>Dashboard: Novo Cliente vs Upsell/Existente</h1>
            <p>WMI Solutions | Gestão de Implantações - Projeto IWN</p>
        </header>

        <div class="tabs">
            <button class="tab-button active" onclick="switchTab(event, 'visao-geral')">Visão Geral Comparativa</button>
            <button class="tab-button" onclick="switchTab(event, 'clientes-novos')">Quadro Clientes Novos</button>
            <button class="tab-button" onclick="switchTab(event, 'upsell')">Quadro Upsell/Existentes</button>
            <button class="tab-button" onclick="switchTab(event, 'comportamento')">Comparativo de Comportamento</button>
            <button class="tab-button" onclick="switchTab(event, 'riscos')">Riscos & Recomendações</button>
        </div>

        <!-- TAB 1: Visão Geral Comparativa -->
        <div id="visao-geral" class="tab-content active">
            <div class="kpi-grid">
                <div class="kpi-card novo">
                    <div class="kpi-label">Total Novo Cliente</div>
                    <div class="kpi-value">{novo_count}</div>
                    <div class="kpi-subtitle">{novo_pct}% do total</div>
                </div>
                <div class="kpi-card upsell">
                    <div class="kpi-label">Total Upsell/Existente</div>
                    <div class="kpi-value">{upsell_count}</div>
                    <div class="kpi-subtitle">{upsell_pct}% do total</div>
                </div>
                <div class="kpi-card success">
                    <div class="kpi-label">Total Geral de Épicos</div>
                    <div class="kpi-value">{total_count}</div>
                    <div class="kpi-subtitle">Todos os status</div>
                </div>
                <div class="kpi-card novo">
                    <div class="kpi-label">Taxa de Conclusão - Novo</div>
                    <div class="kpi-value">{novo_completion}%</div>
                    <div class="kpi-subtitle">Épicos concluídos/cancelados</div>
                </div>
                <div class="kpi-card upsell">
                    <div class="kpi-label">Taxa de Conclusão - Upsell</div>
                    <div class="kpi-value">{upsell_completion}%</div>
                    <div class="kpi-subtitle">Épicos concluídos/cancelados</div>
                </div>
                <div class="kpi-card info">
                    <div class="kpi-label">Hora Média por Épico - Novo</div>
                    <div class="kpi-value">{novo_avg_hours}h</div>
                    <div class="kpi-subtitle">Tempo total gasto</div>
                </div>
                <div class="kpi-card info">
                    <div class="kpi-label">Hora Média por Épico - Upsell</div>
                    <div class="kpi-value">{upsell_avg_hours}h</div>
                    <div class="kpi-subtitle">Tempo total gasto</div>
                </div>
            </div>

            <div class="chart-grid">
                <div class="chart-container full">
                    <div class="chart-title">Distribuição Novo vs Upsell</div>
                    <canvas id="distributionChart"></canvas>
                </div>

                <div class="chart-container">
                    <div class="chart-title">Proporção Novo vs Upsell</div>
                    <canvas id="doughnutChart"></canvas>
                </div>

                <div class="chart-container">
                    <div class="chart-title">Taxa de Conclusão</div>
                    <canvas id="completionChart"></canvas>
                </div>
            </div>
        </div>

        <!-- TAB 2: Quadro Clientes Novos -->
        <div id="clientes-novos" class="tab-content">
            <div class="metric-row">
                <div class="metric-item novo">
                    <div class="metric-label">Épicos Novo - Total</div>
                    <div class="metric-value">{novo_count}</div>
                </div>
                <div class="metric-item novo">
                    <div class="metric-label">Novo - Concluído</div>
                    <div class="metric-value">{metrics['total']['novo']['completed']}</div>
                </div>
                <div class="metric-item novo">
                    <div class="metric-label">Novo - Em Andamento</div>
                    <div class="metric-value">{metrics['total']['novo']['em_andamento']}</div>
                </div>
                <div class="metric-item novo">
                    <div class="metric-label">Novo - Pausado</div>
                    <div class="metric-value">{metrics['total']['novo']['paused']}</div>
                </div>
                <div class="metric-item novo">
                    <div class="metric-label">Novo - Pendente</div>
                    <div class="metric-value">{metrics['total']['novo']['pendente']}</div>
                </div>
            </div>

            <div class="chart-grid">
                <div class="chart-container full">
                    <div class="chart-title">Status de Épicos Novo Cliente por Implantador</div>
                    <canvas id="novosCompletionChart"></canvas>
                </div>
            </div>

            <div class="table-container">
                <table>
                    <thead>
                        <tr>
                            <th>Implantador</th>
                            <th>Total</th>
                            <th>Concluído</th>
                            <th>Em Andamento</th>
                            <th>Pausado</th>
                            <th>Pendente</th>
                            <th>% Conclusão</th>
                            <th>Horas</th>
                        </tr>
                    </thead>
                    <tbody>
"""

    # Add implementer rows for Novo
    for impl in implementer_names:
        impl_data = metrics['implementers'][impl]
        novo_data = impl_data['novo']
        novo_total = novo_data['total']
        novo_completion_rate = calculate_completion_rate(novo_data) if novo_total > 0 else 0

        html += f"""                        <tr>
                            <td><strong>{impl}</strong></td>
                            <td>{novo_total}</td>
                            <td><span class="status-badge status-completed">{novo_data['completed']}</span></td>
                            <td><span class="status-badge status-em-andamento">{novo_data['em_andamento']}</span></td>
                            <td><span class="status-badge status-paused">{novo_data['paused']}</span></td>
                            <td><span class="status-badge status-pendente">{novo_data['pendente']}</span></td>
                            <td>{novo_completion_rate}%</td>
                            <td>{round(impl_data['total_hours'], 1)}h</td>
                        </tr>
"""

    html += """                    </tbody>
                </table>
            </div>
        </div>

        <!-- TAB 3: Quadro Upsell -->
        <div id="upsell" class="tab-content">
            <div class="metric-row">
                <div class="metric-item upsell">
                    <div class="metric-label">Épicos Upsell - Total</div>
                    <div class="metric-value">""" + str(upsell_count) + """</div>
                </div>
                <div class="metric-item upsell">
                    <div class="metric-label">Upsell - Concluído</div>
                    <div class="metric-value">""" + str(metrics['total']['upsell']['completed']) + """</div>
                </div>
                <div class="metric-item upsell">
                    <div class="metric-label">Upsell - Em Andamento</div>
                    <div class="metric-value">""" + str(metrics['total']['upsell']['em_andamento']) + """</div>
                </div>
                <div class="metric-item upsell">
                    <div class="metric-label">Upsell - Pausado</div>
                    <div class="metric-value">""" + str(metrics['total']['upsell']['paused']) + """</div>
                </div>
                <div class="metric-item upsell">
                    <div class="metric-label">Upsell - Pendente</div>
                    <div class="metric-value">""" + str(metrics['total']['upsell']['pendente']) + """</div>
                </div>
            </div>

            <div class="chart-grid">
                <div class="chart-container full">
                    <div class="chart-title">Status de Épicos Upsell/Existente por Implantador</div>
                    <canvas id="upsellCompletionChart"></canvas>
                </div>
            </div>

            <div class="table-container">
                <table>
                    <thead>
                        <tr>
                            <th>Implantador</th>
                            <th>Total</th>
                            <th>Concluído</th>
                            <th>Em Andamento</th>
                            <th>Pausado</th>
                            <th>Pendente</th>
                            <th>% Conclusão</th>
                            <th>Horas</th>
                        </tr>
                    </thead>
                    <tbody>
"""

    # Add implementer rows for Upsell
    for impl in implementer_names:
        impl_data = metrics['implementers'][impl]
        upsell_data = impl_data['upsell']
        upsell_total = upsell_data['total']
        upsell_completion_rate = calculate_completion_rate(upsell_data) if upsell_total > 0 else 0

        html += f"""                        <tr>
                            <td><strong>{impl}</strong></td>
                            <td>{upsell_total}</td>
                            <td><span class="status-badge status-completed">{upsell_data['completed']}</span></td>
                            <td><span class="status-badge status-em-andamento">{upsell_data['em_andamento']}</span></td>
                            <td><span class="status-badge status-paused">{upsell_data['paused']}</span></td>
                            <td><span class="status-badge status-pendente">{upsell_data['pendente']}</span></td>
                            <td>{upsell_completion_rate}%</td>
                            <td>{round(impl_data['total_hours'], 1)}h</td>
                        </tr>
"""

    html += """                    </tbody>
                </table>
            </div>
        </div>

        <!-- TAB 4: Comparativo de Comportamento -->
        <div id="comportamento" class="tab-content">
            <div class="metric-row">
                <div class="metric-item novo">
                    <div class="metric-label">Novo - Em Andamento %</div>
                    <div class="metric-value">""" + str(novo_em_and_pct) + """%</div>
                </div>
                <div class="metric-item upsell">
                    <div class="metric-label">Upsell - Em Andamento %</div>
                    <div class="metric-value">""" + str(upsell_em_and_pct) + """%</div>
                </div>
            </div>

            <div class="chart-grid">
                <div class="chart-container full">
                    <div class="chart-title">Comparativo de Épicos por Implantador: Novo vs Upsell</div>
                    <canvas id="comportamentoChart"></canvas>
                </div>

                <div class="chart-container">
                    <div class="chart-title">Horas Totais por Implantador</div>
                    <canvas id="horasChart"></canvas>
                </div>
            </div>
        </div>

        <!-- TAB 5: Riscos & Recomendações -->
        <div id="riscos" class="tab-content">
            <h2 style="margin-bottom: 20px; color: var(--text-primary);">Análise de Riscos</h2>
"""

    if at_risk_implementers:
        html += """            <div style="margin-bottom: 30px;">
                <h3 style="color: var(--warning); margin-bottom: 15px;">Implantadores em Risco</h3>
"""
        for impl_risk in at_risk_implementers:
            risk_class = 'risk-high' if impl_risk['completion'] < 30 else 'risk-medium'
            html += f"""                <div class="recommendation-box {risk_class}" style="border-left-color: var(--warning);">
                    <h4>{impl_risk['name']}</h4>
                    <p>Taxa de conclusão: <strong>{impl_risk['completion']}%</strong> | Épicos pendentes: <strong>{impl_risk['pending']}</strong></p>
                </div>
"""
        html += """            </div>
"""
    else:
        html += """            <div class="recommendation-box" style="border-left-color: var(--success); background-color: rgba(34, 197, 94, 0.1);">
                <h4 style="color: var(--success);">Status Saudável</h4>
                <p>Todos os implantadores estão com taxas de conclusão acima de 50%. Continuem assim!</p>
            </div>
"""

    html += """
            <h3 style="color: var(--text-primary); margin: 30px 0 15px 0;">Recomendações Estratégicas</h3>

            <div class="recommendation-box">
                <h4>Gestão de Novo Cliente</h4>
                <p>Foco em épicos de novo cliente para expandir a base. Acompanhar de perto o progresso de épicos em andamento para evitar atrasos.</p>
            </div>

            <div class="recommendation-box">
                <h4>Otimização de Upsell</h4>
                <p>Revisar estratégia de implantação para clientes existentes. Avaliar se o tempo médio está alinhado com escopo esperado.</p>
            </div>

            <div class="recommendation-box">
                <h4>Alocação de Recursos</h4>
                <p>Considerar redistribuição de épicos pendentes entre implantadores para equilibrar carga de trabalho.</p>
            </div>

            <div class="recommendation-box">
                <h4>Comunicação com Clientes</h4>
                <p>Aumentar frequência de check-ins para épicos pausados ou escalados. Identificar bloqueios e resolver rapidamente.</p>
            </div>
        </div>

        <footer>
            <p>Dashboard gerado em: <strong>""" + timestamp + """</strong></p>
            <p>Dados sincronizados com Jira Cloud | WMI Solutions</p>
        </footer>
    </div>

    <script>
        // Chart.js color scheme
        const colors = {
            novo: '#06b6d4',
            upsell: '#f97316',
            success: '#22c55e',
            warning: '#f59e0b',
            danger: '#ef4444',
            info: '#3b82f6',
            light: '#c9d1d9',
            dark: '#0d1117'
        };

        const Chart_defaults = Chart.defaults;
        Chart_defaults.color = colors.light;
        Chart_defaults.borderColor = '#30363d';
        Chart_defaults.font.family = '-apple-system, BlinkMacSystemFont, "Segoe UI", "Roboto", "Oxygen"';

        // Data
        const implementers = """ + json.dumps(implementer_names) + """;
        const novoCounts = """ + json.dumps(novo_counts) + """;
        const upsellCounts = """ + json.dumps(upsell_counts) + """;
        const totalHours = """ + json.dumps(total_hours_list) + """;
        const novoCompleted = """ + json.dumps(novo_completed) + """;
        const novoEmAndamento = """ + json.dumps(novo_em_andamento) + """;
        const novoPaused = """ + json.dumps(novo_paused) + """;
        const novoPendente = """ + json.dumps(novo_pendente) + """;
        const upsellCompleted = """ + json.dumps(upsell_completed) + """;
        const upsellEmAndamento = """ + json.dumps(upsell_em_andamento) + """;
        const upsellPaused = """ + json.dumps(upsell_paused) + """;
        const upsellPendente = """ + json.dumps(upsell_pendente) + """;

        // Distribution Chart (Stacked Bar)
        const distributionCtx = document.getElementById('distributionChart');
        new Chart(distributionCtx, {
            type: 'bar',
            data: {
                labels: implementers,
                datasets: [
                    {
                        label: 'Novo Cliente',
                        data: novoCounts,
                        backgroundColor: colors.novo,
                        borderRadius: 4
                    },
                    {
                        label: 'Upsell/Existente',
                        data: upsellCounts,
                        backgroundColor: colors.upsell,
                        borderRadius: 4
                    }
                ]
            },
            options: {
                indexAxis: 'y',
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: {
                        display: true,
                        labels: {
                            color: colors.light,
                            padding: 15
                        }
                    }
                },
                scales: {
                    x: {
                        stacked: true,
                        grid: {
                            color: '#30363d'
                        },
                        ticks: {
                            color: colors.light
                        }
                    },
                    y: {
                        stacked: true,
                        grid: {
                            display: false
                        },
                        ticks: {
                            color: colors.light
                        }
                    }
                }
            }
        });

        // Doughnut Chart
        const doughnutCtx = document.getElementById('doughnutChart');
        new Chart(doughnutCtx, {
            type: 'doughnut',
            data: {
                labels: ['Novo Cliente', 'Upsell/Existente'],
                datasets: [{
                    data: [""" + str(novo_count) + """, """ + str(upsell_count) + """],
                    backgroundColor: [colors.novo, colors.upsell],
                    borderColor: colors.dark,
                    borderWidth: 2
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: {
                        position: 'bottom',
                        labels: {
                            color: colors.light,
                            padding: 15
                        }
                    }
                }
            }
        });

        // Completion Chart
        const completionCtx = document.getElementById('completionChart');
        new Chart(completionCtx, {
            type: 'bar',
            data: {
                labels: ['Novo Cliente', 'Upsell/Existente'],
                datasets: [{
                    label: 'Taxa de Conclusão (%)',
                    data: [""" + str(novo_completion) + """, """ + str(upsell_completion) + """],
                    backgroundColor: [colors.novo, colors.upsell],
                    borderRadius: 4
                }]
            },
            options: {
                indexAxis: 'y',
                responsive: true,
                maintainAspectRatio: false,
                scales: {
                    x: {
                        grid: {
                            color: '#30363d'
                        },
                        ticks: {
                            color: colors.light
                        },
                        max: 100
                    },
                    y: {
                        grid: {
                            display: false
                        },
                        ticks: {
                            color: colors.light
                        }
                    }
                }
            }
        });

        // Novos Completion Chart (Stacked)
        const novosCompletionCtx = document.getElementById('novosCompletionChart');
        new Chart(novosCompletionCtx, {
            type: 'bar',
            data: {
                labels: implementers,
                datasets: [
                    {
                        label: 'Concluído',
                        data: novoCompleted,
                        backgroundColor: colors.success,
                        borderRadius: 4
                    },
                    {
                        label: 'Em Andamento',
                        data: novoEmAndamento,
                        backgroundColor: colors.info,
                        borderRadius: 4
                    },
                    {
                        label: 'Pausado',
                        data: novoPaused,
                        backgroundColor: colors.warning,
                        borderRadius: 4
                    },
                    {
                        label: 'Pendente',
                        data: novoPendente,
                        backgroundColor: colors.danger,
                        borderRadius: 4
                    }
                ]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: {
                        display: true,
                        labels: {
                            color: colors.light,
                            padding: 15
                        }
                    }
                },
                scales: {
                    x: {
                        grid: {
                            color: '#30363d'
                        },
                        ticks: {
                            color: colors.light
                        }
                    },
                    y: {
                        stacked: true,
                        grid: {
                            color: '#30363d'
                        },
                        ticks: {
                            color: colors.light
                        }
                    }
                }
            }
        });

        // Upsell Completion Chart (Stacked)
        const upsellCompletionCtx = document.getElementById('upsellCompletionChart');
        new Chart(upsellCompletionCtx, {
            type: 'bar',
            data: {
                labels: implementers,
                datasets: [
                    {
                        label: 'Concluído',
                        data: upsellCompleted,
                        backgroundColor: colors.success,
                        borderRadius: 4
                    },
                    {
                        label: 'Em Andamento',
                        data: upsellEmAndamento,
                        backgroundColor: colors.info,
                        borderRadius: 4
                    },
                    {
                        label: 'Pausado',
                        data: upsellPaused,
                        backgroundColor: colors.warning,
                        borderRadius: 4
                    },
                    {
                        label: 'Pendente',
                        data: upsellPendente,
                        backgroundColor: colors.danger,
                        borderRadius: 4
                    }
                ]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: {
                        display: true,
                        labels: {
                            color: colors.light,
                            padding: 15
                        }
                    }
                },
                scales: {
                    x: {
                        grid: {
                            color: '#30363d'
                        },
                        ticks: {
                            color: colors.light
                        }
                    },
                    y: {
                        stacked: true,
                        grid: {
                            color: '#30363d'
                        },
                        ticks: {
                            color: colors.light
                        }
                    }
                }
            }
        });

        // Comportamento Chart
        const comportamentoCtx = document.getElementById('comportamentoChart');
        new Chart(comportamentoCtx, {
            type: 'bar',
            data: {
                labels: implementers,
                datasets: [
                    {
                        label: 'Novo Cliente',
                        data: novoCounts,
                        backgroundColor: colors.novo,
                        borderRadius: 4
                    },
                    {
                        label: 'Upsell/Existente',
                        data: upsellCounts,
                        backgroundColor: colors.upsell,
                        borderRadius: 4
                    }
                ]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: {
                        display: true,
                        labels: {
                            color: colors.light,
                            padding: 15
                        }
                    }
                },
                scales: {
                    x: {
                        grid: {
                            color: '#30363d'
                        },
                        ticks: {
                            color: colors.light
                        }
                    },
                    y: {
                        grid: {
                            color: '#30363d'
                        },
                        ticks: {
                            color: colors.light
                        }
                    }
                }
            }
        });

        // Horas Chart
        const horasCtx = document.getElementById('horasChart');
        new Chart(horasCtx, {
            type: 'bar',
            data: {
                labels: implementers,
                datasets: [{
                    label: 'Horas Totais',
                    data: totalHours,
                    backgroundColor: colors.info,
                    borderRadius: 4
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                indexAxis: 'y',
                scales: {
                    x: {
                        grid: {
                            color: '#30363d'
                        },
                        ticks: {
                            color: colors.light
                        }
                    },
                    y: {
                        grid: {
                            display: false
                        },
                        ticks: {
                            color: colors.light
                        }
                    }
                }
            }
        });

        // Tab switching function
        function switchTab(event, tabName) {
            // Hide all tab contents
            const tabContents = document.querySelectorAll('.tab-content');
            tabContents.forEach(content => {
                content.classList.remove('active');
            });

            // Remove active class from all buttons
            const tabButtons = document.querySelectorAll('.tab-button');
            tabButtons.forEach(button => {
                button.classList.remove('active');
            });

            // Show the selected tab content
            document.getElementById(tabName).classList.add('active');

            // Add active class to the clicked button
            event.target.classList.add('active');
        }
    </script>
</body>
</html>"""

    return html

def main():
    """Main execution"""
    try:
        # Initialize Jira client
        jira = JiraClient(JIRA_EMAIL, JIRA_API_TOKEN, JIRA_BASE_URL)

        # Fetch epics
        print("Fetching epics from Jira Cloud...", file=sys.stderr)
        epics = jira.get_epics()
        print(f"Retrieved {len(epics)} epics", file=sys.stderr)

        # Process epics
        print("Processing epics...", file=sys.stderr)
        metrics = process_epics(epics)
        print(f"Processed metrics for {len(IMPLEMENTERS)} implementers", file=sys.stderr)

        # Generate HTML dashboard
        print("Generating HTML dashboard...", file=sys.stderr)
        html_content = generate_html_dashboard(metrics)

        # Write to file
        output_file = os.path.join(os.getcwd(), 'index.html')
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write(html_content)

        print(f"Dashboard generated successfully: {output_file}", file=sys.stderr)
        return 0

    except ValueError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 1

if __name__ == '__main__':
    sys.exit(main())
