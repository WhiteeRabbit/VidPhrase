let button = document.querySelector(".error-box");
const searchForm = document.getElementById('searchForm');
const loadingScreen = document.getElementById('loading-screen');


function highlightWord(text, word) {
    if (!word) return text;

    const regex = new RegExp(`(${word})`, "gi");

    return text.replace(regex, `<span class="highlight">$1</span>`);
}

document.addEventListener("DOMContentLoaded", function () {
    const phraseInput = document.querySelector('input[name="phrase"]');
    const word = phraseInput ? phraseInput.value : "";

    document.querySelectorAll(".results-table tbody tr").forEach(row => {
        const textCell = row.children[2];

        if (textCell && word) {
            textCell.innerHTML = highlightWord(textCell.innerText, word);
        }
    });
});




if (searchForm) {
    searchForm.addEventListener('submit', function(e) {

        if (
            e.submitter &&
            (
                e.submitter.classList.contains('download-btn') ||
                e.submitter.formAction.includes('/download_subtitles')
            )
        ) {
            return;
        }

        const url = document.getElementsByName('video_url')[0].value;
        const phrase = document.getElementsByName('phrase')[0].value;

        if (url && phrase) {
            loadingScreen.style.display = 'flex';
        }
    });
}

if (button && button.textContent.trim() === "Phrase not found") {
    button.style.background = "#2a2511";
    button.style.border = "1px solid #606b1f";
    button.style.color = "#dbeb7c";
}

if (button) {
    button.addEventListener("click", () => {
        button.style.display = "none";
    });
}