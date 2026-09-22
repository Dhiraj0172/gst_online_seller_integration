// Main JavaScript for GST Online Seller

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
