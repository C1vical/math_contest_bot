# Test fixtures

Tests never touch the network. Everything they parse lives here.

## `cemc/` - captured from the live site

Saved from https://cemc.uwaterloo.ca on 2026-11-17 and trimmed only to keep the
repository small:

* contest / solution pages: inline base64 images replaced by a 1x1 PNG and the
  `<style>` block emptied; the markup is otherwise what the site served,
  including its quirks (`2016EuclidContest.html` closes the question list
  early; `2019PascalContest.html` closes `<li>` early for every
  question with a diagram; `2017PascalContest.html` never closes the Part A list,
  so Parts B and C nest inside it; `2016GaloisContest.html` states "Number of Questions: 10" but has 4).
  `2018PascalSolution.html` opens its list twice (`<ol><ol><li>`).
* listing pages: reduced to the results `<table>` and the pager.

The contest content is (c) University of Waterloo / CEMC and is included solely
as parser test input.

## `cms/` - captured from the live site

`comc2005-exam-en.pdf` is the 2005 COMC paper exactly as
https://cms.math.ca/competitions/ served it on 2026-11-26 (unmodified, 112 KB). The content
is (c) Canadian Mathematical Society and is included solely as parser test input.

## `aops_wiki/` and `aops_community/` - hand-written, NOT captured

AoPS answered automated requests with an anti-bot challenge when this was
written, and the project does not work around that. These files are therefore
**synthetic**: written by hand to the documented MediaWiki Action API response
format and to the category-item fields the pre-2.0 scraper consumed
(`item_id`, `item_text`, `item_score`, `post_data.post_rendered`). The maths in
them is invented. They prove the parsers do what they are designed to do; they
do not prove the live site still looks like this. Replace them with real
captures when access is available.
