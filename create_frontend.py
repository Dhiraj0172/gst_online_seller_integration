import os

BASE_DIR = r"C:\Users\ADMIN\gst_online_seller\app"

TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
AUTH_DIR = os.path.join(TEMPLATES_DIR, "auth")
STATIC_CSS = os.path.join(BASE_DIR, "static", "css")
STATIC_JS = os.path.join(BASE_DIR, "static", "js")

os.makedirs(TEMPLATES_DIR, exist_ok=True)
os.makedirs(AUTH_DIR, exist_ok=True)
os.makedirs(STATIC_CSS, exist_ok=True)
os.makedirs(STATIC_JS, exist_ok=True)

files = {}

files[os.path.join(TEMPLATES_DIR, "base.html")] = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{% block title %}GST Online Seller{% endblock %}</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <link rel="stylesheet" href="{{ url_for('static', filename='css/style.css') }}">
</head>
<body>
    <div id="loading-overlay" class="d-none">
        <div class="spinner-border text-primary" role="status">
            <span class="visually-hidden">Loading...</span>
        </div>
    </div>
    
    <div class="wrapper">
        <nav id="sidebar" class="bg-dark text-white">
            <div class="sidebar-header p-3">
                <h4><i class="fas fa-file-invoice-dollar me-2"></i>GST Seller</h4>
            </div>
            <ul class="list-unstyled components p-2">
                <li><a href="{{ url_for('main.dashboard') }}"><i class="fas fa-home me-2"></i>Dashboard</a></li>
                <li><a href="{{ url_for('main.profile') }}"><i class="fas fa-building me-2"></i>GST Profile</a></li>
                <li><a href="{{ url_for('main.import_data') }}"><i class="fas fa-upload me-2"></i>Import Data</a></li>
                <li><a href="{{ url_for('main.statement') }}"><i class="fas fa-table me-2"></i>Manage Data</a></li>
                <li><a href="{{ url_for('main.tcs_reconcile') }}"><i class="fas fa-balance-scale me-2"></i>TCS Reconcile</a></li>
                <li><a href="{{ url_for('main.generate') }}"><i class="fas fa-file-export me-2"></i>Generate GSTR-1</a></li>
                <li><a href="{{ url_for('main.import_history') }}"><i class="fas fa-history me-2"></i>Import History</a></li>
                <li><a href="{{ url_for('main.downloads') }}"><i class="fas fa-download me-2"></i>Downloads</a></li>
                <li><a href="{{ url_for('main.errors') }}"><i class="fas fa-exclamation-triangle me-2"></i>Errors</a></li>
            </ul>
        </nav>

        <div id="content" class="w-100">
            <nav class="navbar navbar-expand-lg navbar-light bg-light">
                <div class="container-fluid">
                    <button type="button" id="sidebarCollapse" class="btn btn-primary">
                        <i class="fas fa-align-left"></i>
                    </button>
                    <div class="d-flex align-items-center ms-auto">
                        <span class="me-3">GSTIN: <strong>{{ current_user.active_gstin|default('Not Selected') }}</strong></span>
                        <select class="form-select form-select-sm me-3" id="period-selector" style="width: auto;">
                            <option value="2023-04">Apr 2023</option>
                            <option value="2023-05">May 2023</option>
                        </select>
                        <div class="dropdown">
                            <a class="nav-link dropdown-toggle" href="#" role="button" data-bs-toggle="dropdown">
                                <i class="fas fa-user-circle fa-lg"></i>
                            </a>
                            <ul class="dropdown-menu dropdown-menu-end">
                                <li><a class="dropdown-item" href="#">Settings</a></li>
                                <li><hr class="dropdown-divider"></li>
                                <li><a class="dropdown-item" href="{{ url_for('auth.logout') }}">Logout</a></li>
                            </ul>
                        </div>
                    </div>
                </div>
            </nav>

            <div class="container-fluid p-4">
                {% with messages = get_flashed_messages(with_categories=true) %}
                    {% if messages %}
                        {% for category, message in messages %}
                            <div class="alert alert-{{ category }} alert-dismissible fade show">
                                {{ message }}
                                <button type="button" class="btn-close" data-bs-dismiss="alert"></button>
                            </div>
                        {% endfor %}
                    {% endif %}
                {% endwith %}
                
                {% block content %}{% endblock %}
            </div>
            
            <footer class="bg-light text-center py-3 mt-auto">
                <p class="mb-0">&copy; 2023 GST Online Seller. All rights reserved.</p>
            </footer>
        </div>
    </div>

    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
    <script src="https://code.jquery.com/jquery-3.6.0.min.js"></script>
    <script src="{{ url_for('static', filename='js/main.js') }}"></script>
    {% block scripts %}{% endblock %}
</body>
</html>
"""

files[os.path.join(AUTH_DIR, "login.html")] = """{% extends 'base.html' %}
{% block title %}Login - GST Online Seller{% endblock %}
{% block content %}
<div class="row justify-content-center mt-5">
    <div class="col-md-4">
        <div class="card shadow">
            <div class="card-body p-4">
                <h3 class="text-center mb-4">Login</h3>
                <form method="POST" action="{{ url_for('auth.login') }}">
                    <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
                    <div class="mb-3">
                        <label>Email address</label>
                        <input type="email" name="email" class="form-control" required>
                    </div>
                    <div class="mb-3">
                        <label>Password</label>
                        <input type="password" name="password" class="form-control" required>
                    </div>
                    <button type="submit" class="btn btn-primary w-100">Login</button>
                </form>
                <div class="mt-3 text-center">
                    <a href="{{ url_for('auth.register') }}">Don't have an account? Register</a>
                </div>
            </div>
        </div>
    </div>
</div>
{% endblock %}
"""

files[os.path.join(AUTH_DIR, "register.html")] = """{% extends 'base.html' %}
{% block title %}Register - GST Online Seller{% endblock %}
{% block content %}
<div class="row justify-content-center mt-5">
    <div class="col-md-4">
        <div class="card shadow">
            <div class="card-body p-4">
                <h3 class="text-center mb-4">Register</h3>
                <form method="POST" action="{{ url_for('auth.register') }}">
                    <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
                    <div class="mb-3">
                        <label>Name</label>
                        <input type="text" name="name" class="form-control" required>
                    </div>
                    <div class="mb-3">
                        <label>Email address</label>
                        <input type="email" name="email" class="form-control" required>
                    </div>
                    <div class="mb-3">
                        <label>Password</label>
                        <input type="password" name="password" class="form-control" required>
                    </div>
                    <button type="submit" class="btn btn-primary w-100">Register</button>
                </form>
                <div class="mt-3 text-center">
                    <a href="{{ url_for('auth.login') }}">Already have an account? Login</a>
                </div>
            </div>
        </div>
    </div>
</div>
{% endblock %}
"""

files[os.path.join(TEMPLATES_DIR, "dashboard.html")] = """{% extends 'base.html' %}
{% block title %}Dashboard - GST Online Seller{% endblock %}
{% block content %}
<div class="d-flex justify-content-between align-items-center mb-4">
    <h2>Dashboard</h2>
    <div>
        <a href="{{ url_for('main.import_data') }}" class="btn btn-primary"><i class="fas fa-upload me-1"></i> Import Data</a>
        <a href="{{ url_for('main.generate') }}" class="btn btn-success"><i class="fas fa-file-export me-1"></i> Generate GSTR-1</a>
    </div>
</div>

<div class="row mb-4">
    <div class="col-md-3">
        <div class="card bg-primary text-white shadow h-100">
            <div class="card-body">
                <h6>Total Invoices</h6>
                <h3>{{ stats.total_invoices|default(0) }}</h3>
            </div>
        </div>
    </div>
    <div class="col-md-3">
        <div class="card bg-info text-white shadow h-100">
            <div class="card-body">
                <h6>Taxable Value</h6>
                <h3>₹{{ stats.taxable_value|default('0.00') }}</h3>
            </div>
        </div>
    </div>
    <div class="col-md-3">
        <div class="card bg-warning text-white shadow h-100">
            <div class="card-body">
                <h6>Total Tax</h6>
                <h3>₹{{ stats.total_tax|default('0.00') }}</h3>
            </div>
        </div>
    </div>
    <div class="col-md-3">
        <div class="card bg-success text-white shadow h-100">
            <div class="card-body">
                <h6>GSTR-1 Status</h6>
                <h3>{{ stats.gstr1_status|default('Pending') }}</h3>
            </div>
        </div>
    </div>
</div>

<div class="row">
    <div class="col-md-6 mb-4">
        <div class="card shadow h-100">
            <div class="card-header bg-white">
                <h5 class="mb-0">Classification Breakdown</h5>
            </div>
            <div class="card-body">
                <ul class="list-group list-group-flush">
                    <li class="list-group-item d-flex justify-content-between align-items-center">
                        B2B Invoices
                        <span class="badge bg-primary rounded-pill">{{ breakdown.b2b|default(0) }}</span>
                    </li>
                    <li class="list-group-item d-flex justify-content-between align-items-center">
                        B2C Invoices
                        <span class="badge bg-primary rounded-pill">{{ breakdown.b2c|default(0) }}</span>
                    </li>
                    <li class="list-group-item d-flex justify-content-between align-items-center">
                        CDNR
                        <span class="badge bg-primary rounded-pill">{{ breakdown.cdnr|default(0) }}</span>
                    </li>
                    <li class="list-group-item d-flex justify-content-between align-items-center">
                        Nil/Exempt
                        <span class="badge bg-primary rounded-pill">{{ breakdown.nil|default(0) }}</span>
                    </li>
                </ul>
            </div>
        </div>
    </div>
    <div class="col-md-6 mb-4">
        <div class="card shadow h-100">
            <div class="card-header bg-white">
                <h5 class="mb-0">Tax Summary</h5>
            </div>
            <div class="card-body">
                <ul class="list-group list-group-flush">
                    <li class="list-group-item d-flex justify-content-between align-items-center">
                        CGST
                        <span>₹{{ tax_summary.cgst|default('0.00') }}</span>
                    </li>
                    <li class="list-group-item d-flex justify-content-between align-items-center">
                        SGST
                        <span>₹{{ tax_summary.sgst|default('0.00') }}</span>
                    </li>
                    <li class="list-group-item d-flex justify-content-between align-items-center">
                        IGST
                        <span>₹{{ tax_summary.igst|default('0.00') }}</span>
                    </li>
                    <li class="list-group-item d-flex justify-content-between align-items-center">
                        Cess
                        <span>₹{{ tax_summary.cess|default('0.00') }}</span>
                    </li>
                </ul>
            </div>
        </div>
    </div>
</div>
{% endblock %}
"""

files[os.path.join(TEMPLATES_DIR, "profile.html")] = """{% extends 'base.html' %}
{% block title %}GST Profile - GST Online Seller{% endblock %}
{% block content %}
<div class="row">
    <div class="col-md-4">
        <div class="card shadow">
            <div class="card-header bg-white">
                <h5 class="mb-0">Add/Edit Profile</h5>
            </div>
            <div class="card-body">
                <form id="profile-form" method="POST">
                    <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
                    <div class="mb-3">
                        <label class="form-label">GSTIN</label>
                        <input type="text" class="form-control" id="gstin" name="gstin" maxlength="15" required>
                        <div class="invalid-feedback">Invalid GSTIN format.</div>
                    </div>
                    <div class="mb-3">
                        <label class="form-label">Legal Name</label>
                        <input type="text" class="form-control" name="legal_name" required>
                    </div>
                    <div class="mb-3">
                        <label class="form-label">Trade Name</label>
                        <input type="text" class="form-control" name="trade_name">
                    </div>
                    <div class="mb-3">
                        <label class="form-label">State</label>
                        <select class="form-select" id="state" name="state" required>
                            <option value="">Select State</option>
                            <option value="27">Maharashtra (27)</option>
                            <option value="29">Karnataka (29)</option>
                            <!-- More states -->
                        </select>
                    </div>
                    <div class="mb-3">
                        <label class="form-label">Filing Frequency</label>
                        <select class="form-select" name="frequency">
                            <option value="Monthly">Monthly</option>
                            <option value="Quarterly">Quarterly</option>
                        </select>
                    </div>
                    <button type="submit" class="btn btn-primary w-100">Save Profile</button>
                </form>
            </div>
        </div>
    </div>
    <div class="col-md-8">
        <div class="card shadow">
            <div class="card-header bg-white">
                <h5 class="mb-0">Saved Profiles</h5>
            </div>
            <div class="card-body">
                <div class="table-responsive">
                    <table class="table table-hover">
                        <thead>
                            <tr>
                                <th>GSTIN</th>
                                <th>Legal Name</th>
                                <th>State</th>
                                <th>Actions</th>
                            </tr>
                        </thead>
                        <tbody>
                            {% for profile in profiles|default([]) %}
                            <tr>
                                <td>{{ profile.gstin }}</td>
                                <td>{{ profile.legal_name }}</td>
                                <td>{{ profile.state }}</td>
                                <td>
                                    <button class="btn btn-sm btn-outline-primary"><i class="fas fa-edit"></i></button>
                                    <button class="btn btn-sm btn-outline-danger"><i class="fas fa-trash"></i></button>
                                </td>
                            </tr>
                            {% else %}
                            <tr><td colspan="4" class="text-center">No profiles found.</td></tr>
                            {% endfor %}
                        </tbody>
                    </table>
                </div>
            </div>
        </div>
    </div>
</div>
{% endblock %}
"""

files[os.path.join(TEMPLATES_DIR, "import.html")] = """{% extends 'base.html' %}
{% block title %}Import Data - GST Online Seller{% endblock %}
{% block content %}
<h2 class="mb-4">Import Data</h2>

<div class="row g-4">
    {% set platforms = ['Amazon', 'Flipkart', 'Meesho', 'Myntra'] %}
    {% for p in platforms %}
    <div class="col-md-3">
        <div class="card shadow h-100 text-center">
            <div class="card-body">
                <i class="fas fa-shopping-cart fa-3x mb-3 text-primary"></i>
                <h5 class="card-title">{{ p }}</h5>
                <p class="text-muted small">Supports Excel/CSV</p>
                <button class="btn btn-outline-primary btn-sm mt-2 w-100" onclick="showImportModal('{{ p }}')">Upload {{ p }} Data</button>
            </div>
        </div>
    </div>
    {% endfor %}
</div>

<!-- Import Modal -->
<div class="modal fade" id="importModal" tabindex="-1">
    <div class="modal-dialog modal-lg">
        <div class="modal-content">
            <div class="modal-header">
                <h5 class="modal-title">Upload <span id="platformName"></span> Data</h5>
                <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
            </div>
            <div class="modal-body">
                <div class="upload-zone p-5 border border-primary border-2 border-dashed text-center rounded bg-light mb-3" id="drop-zone">
                    <i class="fas fa-cloud-upload-alt fa-3x text-primary mb-3"></i>
                    <h5>Drag and drop file here or click to select</h5>
                    <input type="file" id="fileInput" class="d-none" accept=".csv, .xlsx, .xls">
                </div>
                <div class="progress d-none" id="uploadProgress">
                    <div class="progress-bar progress-bar-striped progress-bar-animated" style="width: 0%"></div>
                </div>
                <div id="previewArea" class="d-none mt-4">
                    <h6>Preview</h6>
                    <!-- Preview content goes here -->
                </div>
            </div>
            <div class="modal-footer">
                <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Cancel</button>
                <button type="button" class="btn btn-primary d-none" id="confirmImportBtn">Confirm Import</button>
            </div>
        </div>
    </div>
</div>
{% endblock %}
"""

files[os.path.join(TEMPLATES_DIR, "statement.html")] = """{% extends 'base.html' %}
{% block title %}Manage Data - GST Online Seller{% endblock %}
{% block content %}
<h2 class="mb-4">Manage Statement Data</h2>

<ul class="nav nav-tabs mb-3" id="statementTabs">
    <li class="nav-item"><a class="nav-link active" data-bs-toggle="tab" href="#b2b">B2B</a></li>
    <li class="nav-item"><a class="nav-link" data-bs-toggle="tab" href="#b2c">B2C</a></li>
    <li class="nav-item"><a class="nav-link" data-bs-toggle="tab" href="#cdnr">CDNR</a></li>
    <li class="nav-item"><a class="nav-link" data-bs-toggle="tab" href="#hsn">HSN</a></li>
</ul>

<div class="tab-content">
    <div class="tab-pane fade show active" id="b2b">
        <div class="card shadow">
            <div class="card-body">
                <div class="d-flex justify-content-between mb-3">
                    <div>
                        <button class="btn btn-sm btn-outline-secondary">Export</button>
                        <button class="btn btn-sm btn-outline-danger">Bulk Delete</button>
                    </div>
                    <input type="text" class="form-control form-control-sm w-25" placeholder="Search...">
                </div>
                <div class="table-responsive">
                    <table class="table table-bordered table-hover align-middle">
                        <thead class="table-light">
                            <tr>
                                <th><input type="checkbox"></th>
                                <th>Recipient GSTIN</th>
                                <th>Invoice No</th>
                                <th>Date</th>
                                <th>Value</th>
                                <th>Status</th>
                                <th>Actions</th>
                            </tr>
                        </thead>
                        <tbody>
                            <!-- Data rows -->
                            <tr>
                                <td colspan="7" class="text-center">No data available</td>
                            </tr>
                        </tbody>
                    </table>
                </div>
                <!-- Pagination -->
            </div>
        </div>
    </div>
    <!-- Other tabs would be similar -->
</div>
{% endblock %}
"""

files[os.path.join(TEMPLATES_DIR, "tcs_reconcile.html")] = """{% extends 'base.html' %}
{% block title %}TCS Reconcile - GST Online Seller{% endblock %}
{% block content %}
<h2 class="mb-4">TCS Reconciliation</h2>
<div class="card shadow mb-4">
    <div class="card-body">
        <div class="row align-items-end">
            <div class="col-md-6">
                <label class="form-label">Upload Portal TCS Report</label>
                <input type="file" class="form-control">
            </div>
            <div class="col-md-2">
                <button class="btn btn-primary w-100">Reconcile</button>
            </div>
        </div>
    </div>
</div>
<div class="card shadow">
    <div class="card-body">
        <div class="table-responsive">
            <table class="table">
                <thead>
                    <tr>
                        <th>State</th>
                        <th>Our Taxable</th>
                        <th>Portal Taxable</th>
                        <th>Our TCS</th>
                        <th>Portal TCS</th>
                        <th>Status</th>
                    </tr>
                </thead>
                <tbody>
                    <tr><td colspan="6" class="text-center">No reconciliation data</td></tr>
                </tbody>
            </table>
        </div>
    </div>
</div>
{% endblock %}
"""

files[os.path.join(TEMPLATES_DIR, "generate.html")] = """{% extends 'base.html' %}
{% block title %}Generate GSTR-1 - GST Online Seller{% endblock %}
{% block content %}
<h2 class="mb-4">Generate GSTR-1</h2>
<div class="row">
    <div class="col-md-6">
        <div class="card shadow">
            <div class="card-body">
                <h5 class="card-title">Pre-generation Checklist</h5>
                <ul class="list-group list-group-flush mb-3">
                    <li class="list-group-item d-flex justify-content-between align-items-center">
                        Errors resolved
                        <span class="badge bg-success rounded-pill"><i class="fas fa-check"></i></span>
                    </li>
                    <li class="list-group-item d-flex justify-content-between align-items-center">
                        TCS Reconciled
                        <span class="badge bg-warning rounded-pill"><i class="fas fa-exclamation"></i></span>
                    </li>
                </ul>
                <div class="form-check mb-3">
                    <input class="form-check-input" type="checkbox" id="includeHsn" checked>
                    <label class="form-check-label" for="includeHsn">Include HSN details</label>
                </div>
                <button class="btn btn-success w-100" id="generateBtn">Generate Return Files</button>
            </div>
        </div>
    </div>
    <div class="col-md-6">
        <div class="card shadow h-100" id="resultCard" style="display: none;">
            <div class="card-body text-center">
                <h5 class="card-title text-success"><i class="fas fa-check-circle fa-2x mb-2"></i><br>Generation Complete</h5>
                <div class="mt-4">
                    <a href="#" class="btn btn-outline-primary mb-2 w-75"><i class="fas fa-file-excel me-2"></i>Download Excel</a>
                    <a href="#" class="btn btn-outline-info mb-2 w-75"><i class="fas fa-file-code me-2"></i>Download JSON</a>
                </div>
            </div>
        </div>
    </div>
</div>
{% endblock %}
"""

files[os.path.join(TEMPLATES_DIR, "import_history.html")] = """{% extends 'base.html' %}
{% block title %}Import History - GST Online Seller{% endblock %}
{% block content %}
<h2 class="mb-4">Import History</h2>
<div class="card shadow">
    <div class="card-body">
        <div class="table-responsive">
            <table class="table">
                <thead>
                    <tr>
                        <th>Date</th>
                        <th>Platform</th>
                        <th>File Name</th>
                        <th>Status</th>
                        <th>Rows Processed</th>
                        <th>Actions</th>
                    </tr>
                </thead>
                <tbody>
                    <tr><td colspan="6" class="text-center">No history available</td></tr>
                </tbody>
            </table>
        </div>
    </div>
</div>
{% endblock %}
"""

files[os.path.join(TEMPLATES_DIR, "downloads.html")] = """{% extends 'base.html' %}
{% block title %}Downloads - GST Online Seller{% endblock %}
{% block content %}
<h2 class="mb-4">Downloads</h2>
<div class="card shadow">
    <div class="card-body">
        <div class="table-responsive">
            <table class="table">
                <thead>
                    <tr>
                        <th>Generated At</th>
                        <th>Period</th>
                        <th>Type</th>
                        <th>Size</th>
                        <th>Action</th>
                    </tr>
                </thead>
                <tbody>
                    <tr><td colspan="5" class="text-center">No downloads available</td></tr>
                </tbody>
            </table>
        </div>
    </div>
</div>
{% endblock %}
"""

files[os.path.join(TEMPLATES_DIR, "errors.html")] = """{% extends 'base.html' %}
{% block title %}Errors - GST Online Seller{% endblock %}
{% block content %}
<h2 class="mb-4">Validation Errors</h2>
<div class="card shadow">
    <div class="card-body">
        <div class="table-responsive">
            <table class="table">
                <thead>
                    <tr>
                        <th>Invoice No</th>
                        <th>Field</th>
                        <th>Error</th>
                        <th>Severity</th>
                        <th>Suggested Fix</th>
                    </tr>
                </thead>
                <tbody>
                    <tr><td colspan="5" class="text-center">No errors found</td></tr>
                </tbody>
            </table>
        </div>
    </div>
</div>
{% endblock %}
"""

files[os.path.join(STATIC_CSS, "style.css")] = """/* Custom Styles for GST Online Seller */
:root {
    --primary-color: #1a73e8;
    --sidebar-width: 250px;
}

body {
    background-color: #f4f6f9;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
}

.wrapper {
    display: flex;
    width: 100%;
    align-items: stretch;
    min-height: 100vh;
}

#sidebar {
    min-width: var(--sidebar-width);
    max-width: var(--sidebar-width);
    transition: all 0.3s;
    background-color: #2c3e50 !important;
}

#sidebar.active {
    margin-left: calc(var(--sidebar-width) * -1);
}

#sidebar .sidebar-header {
    background: #1a252f;
}

#sidebar ul.components {
    padding: 20px 0;
}

#sidebar ul p {
    color: #fff;
    padding: 10px;
}

#sidebar ul li a {
    padding: 15px 20px;
    font-size: 1.1em;
    display: block;
    color: #ecf0f1;
    text-decoration: none;
    transition: 0.2s;
}

#sidebar ul li a:hover {
    color: #fff;
    background: var(--primary-color);
}

#content {
    width: calc(100% - var(--sidebar-width));
    min-height: 100vh;
    transition: all 0.3s;
    display: flex;
    flex-direction: column;
}

.card {
    border: none;
    border-radius: 8px;
    box-shadow: 0 4px 6px rgba(0,0,0,0.05) !important;
}

#loading-overlay {
    position: fixed;
    top: 0;
    left: 0;
    width: 100%;
    height: 100%;
    background: rgba(255,255,255,0.8);
    z-index: 9999;
    display: flex;
    justify-content: center;
    align-items: center;
}

.border-dashed {
    border-style: dashed !important;
}

.upload-zone {
    cursor: pointer;
    transition: 0.3s;
}

.upload-zone:hover {
    background-color: #e9ecef !important;
}

@media (max-width: 768px) {
    #sidebar {
        margin-left: calc(var(--sidebar-width) * -1);
    }
    #sidebar.active {
        margin-left: 0;
    }
    #content {
        width: 100%;
    }
}
"""

files[os.path.join(STATIC_JS, "main.js")] = """// Main JavaScript for GST Online Seller

$(document).ready(function () {
    // Sidebar toggle
    $('#sidebarCollapse').on('click', function () {
        $('#sidebar').toggleClass('active');
    });

    // GSTIN Validation function
    function validateGSTIN(gstin) {
        const regex = /^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z]{1}[1-9A-Z]{1}Z[0-9A-Z]{1}$/;
        return regex.test(gstin);
    }

    $('#gstin').on('input', function() {
        const val = $(this).val().toUpperCase();
        $(this).val(val);
        if(val.length === 15) {
            if(validateGSTIN(val)) {
                $(this).removeClass('is-invalid').addClass('is-valid');
            } else {
                $(this).removeClass('is-valid').addClass('is-invalid');
            }
        }
    });

    // Upload zone drag and drop
    const dropZone = document.getElementById('drop-zone');
    const fileInput = document.getElementById('fileInput');

    if (dropZone) {
        dropZone.addEventListener('click', () => fileInput.click());

        dropZone.addEventListener('dragover', (e) => {
            e.preventDefault();
            dropZone.classList.add('bg-light');
        });

        dropZone.addEventListener('dragleave', () => {
            dropZone.classList.remove('bg-light');
        });

        dropZone.addEventListener('drop', (e) => {
            e.preventDefault();
            dropZone.classList.remove('bg-light');
            if (e.dataTransfer.files.length) {
                fileInput.files = e.dataTransfer.files;
                handleFileSelect();
            }
        });

        fileInput.addEventListener('change', handleFileSelect);
    }

    function handleFileSelect() {
        if(fileInput.files.length > 0) {
            $('#uploadProgress').removeClass('d-none');
            $('#confirmImportBtn').removeClass('d-none');
            // Simulate progress
            let progress = 0;
            const interval = setInterval(() => {
                progress += 10;
                $('.progress-bar').css('width', progress + '%');
                if(progress >= 100) {
                    clearInterval(interval);
                    $('#previewArea').removeClass('d-none').html('<p class="text-success">File read successfully. Ready to import.</p>');
                }
            }, 200);
        }
    }

    $('#generateBtn').on('click', function() {
        const btn = $(this);
        btn.prop('disabled', true).html('<i class="fas fa-spinner fa-spin me-2"></i>Generating...');
        
        // Simulate API call
        setTimeout(() => {
            $('#resultCard').show();
            btn.html('Generate Return Files').prop('disabled', false);
        }, 2000);
    });
});

function showImportModal(platform) {
    $('#platformName').text(platform);
    $('#importModal').modal('show');
}

function showLoading() {
    $('#loading-overlay').removeClass('d-none');
}

function hideLoading() {
    $('#loading-overlay').addClass('d-none');
}
"""

for path, content in files.items():
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

print("Created all frontend files successfully.")
