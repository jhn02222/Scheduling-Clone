#!/usr/bin/env python3
"""
Interactive Schedule Visualization using Plotly
Creates an HTML dashboard with hover tooltips, filters, and better readability
"""

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import plotly.offline as pyo
from collections import defaultdict
import webbrowser
import os

def create_interactive_dashboard(csv_path, output_html="schedule_dashboard.html"):
    """Create an interactive HTML dashboard from the schedule CSV"""
    
    # Load the schedule data
    df = pd.read_csv(csv_path)
    
    # Create color mapping for courses
    course_colors = {
        1113: '#f472b6',  # pink
        2250: '#4ade80',  # green  
        2260: '#a78bfa',  # purple
        2270: '#fb923c',  # orange
        2500: '#60a5fa',  # blue
        2700: '#34d399',  # teal
        3300: '#fbbf24',  # amber
    }
    
    df['color'] = df['Course'].map(course_colors).fillna('#94a3b8')
    
    # Create the main figure with subplots
    fig = make_subplots(
        rows=2, cols=2,
        subplot_titles=('Schedule Overview', 'Course Distribution', 
                       'Instructor Workload', 'Room Utilization'),
        specs=[[{"type": "scatter"}, {"type": "bar"}],
               [{"type": "bar"}, {"type": "bar"}]],
        vertical_spacing=0.08,
        horizontal_spacing=0.05
    )
    
    # 1. Schedule Overview (Scatter plot)
    for option in df['Option'].unique():
        option_data = df[df['Option'] == option]
        fig.add_trace(
            go.Scatter(
                x=option_data['Block_Label'],
                y=option_data['Course'],
                mode='markers',
                marker=dict(
                    size=12,
                    color=option_data['color'],
                    line=dict(width=2, color='white')
                ),
                text=option_data.apply(lambda row: 
                    f"<b>{row['Course']} - {row['Title']}</b><br>"
                    f"Instructor: {row['Instructor']}<br>"
                    f"Room: {row['Room']}<br>"
                    f"CRN: {row['CRN']}<br>"
                    f"Days: {row['Days']}<br>"
                    f"Time: {row['Block_Label']}<br>"
                    f"Fill: {row['Expected_Fill_Pct']}%", axis=1),
                hovertemplate="%{text}<extra></extra>",
                name=option,
                showlegend=True
            ),
            row=1, col=1
        )
    
    # 2. Course Distribution (Bar chart)
    course_counts = df.groupby('Course').size().reset_index(name='Count')
    fig.add_trace(
        go.Bar(
            x=course_counts['Course'],
            y=course_counts['Count'],
            marker_color=[course_colors.get(c, '#94a3b8') for c in course_counts['Course']],
            name='Course Count',
            showlegend=False
        ),
        row=1, col=2
    )
    
    # 3. Instructor Workload (Bar chart)
    instructor_counts = df[df['Instructor'] != 'TBA'].groupby('Instructor').size().reset_index(name='Sections')
    instructor_counts = instructor_counts.sort_values('Sections', ascending=False).head(10)
    fig.add_trace(
        go.Bar(
            x=instructor_counts['Sections'],
            y=instructor_counts['Instructor'],
            orientation='h',
            marker_color='#60a5fa',
            name='Instructor Load',
            showlegend=False
        ),
        row=2, col=1
    )
    
    # 4. Room Utilization (Bar chart)
    room_counts = df.groupby('Room').size().reset_index(name='Classes')
    room_counts = room_counts.sort_values('Classes', ascending=False).head(10)
    fig.add_trace(
        go.Bar(
            x=room_counts['Classes'],
            y=room_counts['Room'],
            orientation='h',
            marker_color='#34d399',
            name='Room Usage',
            showlegend=False
        ),
        row=2, col=2
    )
    
    # Update layout
    fig.update_layout(
        title={
            'text': '<b>Math Department Schedule Interactive Dashboard</b>',
            'x': 0.5,
            'xanchor': 'center',
            'font': {'size': 20}
        },
        height=800,
        showlegend=True,
        template='plotly_white',
        font=dict(family="Arial, sans-serif", size=10)
    )
    
    # Update axes labels
    fig.update_xaxes(title_text="Time Block", row=1, col=1)
    fig.update_yaxes(title_text="Course", row=1, col=1)
    fig.update_xaxes(title_text="Course", row=1, col=2)
    fig.update_yaxes(title_text="Number of Sections", row=1, col=2)
    fig.update_xaxes(title_text="Sections", row=2, col=1)
    fig.update_yaxes(title_text="Instructor", row=2, col=1)
    fig.update_xaxes(title_text="Classes", row=2, col=2)
    fig.update_yaxes(title_text="Room", row=2, col=2)
    
    # Save the HTML file
    pyo.plot(fig, filename=output_html, auto_open=False)
    
    # Also create a detailed table view
    create_detailed_table_view(df, output_html.replace('.html', '_table.html'))
    
    print(f"Interactive dashboard created: {output_html}")
    print(f"Detailed table view created: {output_html.replace('.html', '_table.html')}")
    
    return output_html

def create_detailed_table_view(df, output_html):
    """Create a detailed searchable table view"""
    
    # Create HTML with embedded CSS and JavaScript
    html_content = f"""
<!DOCTYPE html>
<html>
<head>
    <title>Math Schedule - Detailed Table View</title>
    <style>
        body {{ font-family: Arial, sans-serif; margin: 20px; }}
        .search-box {{ padding: 10px; margin-bottom: 20px; }}
        .search-input {{ width: 300px; padding: 8px; border: 1px solid #ddd; }}
        table {{ border-collapse: collapse; width: 100%; }}
        th, td {{ border: 1px solid #ddd; padding: 8px; text-align: left; }}
        th {{ background-color: #f2f2f2; position: sticky; top: 0; }}
        tr:nth-child(even) {{ background-color: #f9f9f9; }}
        .course-1113 {{ background-color: #fce7f3; }}
        .course-2250 {{ background-color: #dcfce7; }}
        .course-2260 {{ background-color: #ede9fe; }}
        .course-2270 {{ background-color: #fed7aa; }}
        .course-2500 {{ background-color: #dbeafe; }}
        .course-2700 {{ background-color: #ccfbf1; }}
        .course-3300 {{ background-color: #fef3c7; }}
        .moved {{ background-color: #fee2e2; }}
    </style>
</head>
<body>
    <h1>Math Department Schedule - Detailed View</h1>
    
    <div class="search-box">
        <input type="text" class="search-input" id="searchInput" 
               placeholder="Search by course, instructor, room, CRN...">
        <button onclick="searchTable()">Search</button>
        <button onclick="clearSearch()">Clear</button>
    </div>
    
    <table id="scheduleTable">
        <thead>
            <tr>
                <th>Option</th>
                <th>Course</th>
                <th>Title</th>
                <th>Instructor</th>
                <th>CRN</th>
                <th>Days</th>
                <th>Time</th>
                <th>Room</th>
                <th>Capacity</th>
                <th>Expected Fill %</th>
                <th>Moved</th>
            </tr>
        </thead>
        <tbody>
"""
    
    # Add table rows
    for _, row in df.iterrows():
        moved_class = "moved" if row['Moved_From_Skeleton'] == "YES" else ""
        course_class = f"course-{row['Course']}"
        
        html_content += f"""
            <tr class="{course_class} {moved_class}">
                <td>{row['Option']}</td>
                <td><b>MATH {row['Course']}</b></td>
                <td>{row['Title']}</td>
                <td>{row['Instructor']}</td>
                <td>{row['CRN']}</td>
                <td>{row['Days']}</td>
                <td>{row['Block_Label']}</td>
                <td>{row['Room']}</td>
                <td>{row['Section_Capacity']}</td>
                <td>{row['Expected_Fill_Pct']}%</td>
                <td>{row['Moved_From_Skeleton']}</td>
            </tr>
"""
    
    html_content += """
        </tbody>
    </table>
    
    <script>
        function searchTable() {
            const input = document.getElementById("searchInput");
            const filter = input.value.toUpperCase();
            const table = document.getElementById("scheduleTable");
            const rows = table.getElementsByTagName("tr");
            
            for (let i = 1; i < rows.length; i++) {
                const cells = rows[i].getElementsByTagName("td");
                let found = false;
                
                for (let j = 0; j < cells.length; j++) {
                    const cellText = cells[j].textContent || cells[j].innerText;
                    if (cellText.toUpperCase().indexOf(filter) > -1) {
                        found = true;
                        break;
                    }
                }
                
                rows[i].style.display = found ? "" : "none";
            }
        }
        
        function clearSearch() {
            document.getElementById("searchInput").value = "";
            searchTable();
        }
        
        // Enable search on Enter key
        document.getElementById("searchInput").addEventListener("keyup", function(event) {
            if (event.key === "Enter") {
                searchTable();
            }
        });
    </script>
</body>
</html>
"""
    
    with open(output_html, 'w', encoding='utf-8') as f:
        f.write(html_content)

if __name__ == "__main__":
    # Create the interactive dashboard
    csv_path = "data/schedule_output.csv"
    if os.path.exists(csv_path):
        dashboard_path = create_interactive_dashboard(csv_path)
        print(f"\nOpen {dashboard_path} in your browser to view the interactive dashboard.")
        print("The dashboard includes:")
        print("- Hover tooltips with full course details")
        print("- Multiple visualization perspectives")
        print("- Searchable detailed table view")
        print("- Much better readability than static PNGs")
    else:
        print(f"CSV file not found: {csv_path}")
        print("Please run the main scheduler first to generate the output CSV.")
